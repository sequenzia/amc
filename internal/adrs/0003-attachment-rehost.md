# ADR 0003: Attachments are re-hosted by the adapter

**Status**: Accepted
**Date**: 2026-05-03

## Context

Inbound messages on the two v1 platforms carry attachments that are reachable by very different mechanisms:

- **Discord**: every attachment has a CDN URL (`https://cdn.discordapp.com/attachments/...`). These URLs are **signed and time-limited** — they expire (currently 24 hours after the signature is minted). After expiry the URL returns 403, and the only way to re-fetch is to re-call the Discord REST API with the original message ID and grab the freshly-signed URL from the response.
- **iMessage**: attachments are local files at paths like `~/Library/Messages/Attachments/...`. The path is stable across restarts, but it is a **filesystem path on one specific Mac**, not a URL. Anything outside the adapter process (an agent runtime, a webhook receiver, a developer running `curl` from a laptop) cannot dereference it.

The normalized envelope (blueprint §3) ships an `attachments[].url` field that the agent is expected to fetch from. There are two ways to populate it:

- **Option A — pass-through.** Put the Discord CDN URL directly in the envelope; put `file://` for iMessage paths. Cheap, but the agent gets a value it cannot reliably use: Discord URLs expire, file URLs only work from the same Mac.
- **Option B — re-host.** The adapter pulls the bytes (HTTP GET for Discord, filesystem read for iMessage) into a local store, assigns a stable `att_<ULID>` ID, persists `(id, message_id, mime, size_bytes, bytes_path, original_url_or_path)` in an `attachments` table, and exposes the file at `GET /attachments/{id}` with the same bearer auth as the rest of the API.

This is exactly the kind of decision the blueprint flagged in §9 ("Attachment handling"). The user's framing of REQ-AMG-008 made it concrete: the agent needs **stable URLs across replays** so that context fetches days later can still serve the bytes.

## Decision

The adapter **re-hosts** every inbound attachment.

- On message ingest, each attachment is downloaded (Discord) or copied (iMessage) into `AMG_ATTACHMENT_DIR` (default `~/Library/Application Support/messaging-agent/attachments/`), with the original bytes preserved verbatim.
- Each re-hosted file gets an `att_<ULID>` ID, persisted as a row in the `attachments` table with `bytes_path` pointing at the local copy and `original_url_or_path` retained for forensics.
- The `attachments[].url` field on the envelope is **always** the adapter URL: `http://<bind>/attachments/{id}`, served by an authenticated route that streams from `bytes_path`.
- Outbound `attachments[]` (in `POST /messages/send`) accept either a URL or a path. The adapter re-hosts before delivery: it copies to `AMG_ATTACHMENT_DIR`, assigns an `att_` ID, then hands the bytes to the platform connector. This means an outbound attachment is queryable via `GET /attachments/{id}` after the send completes.

If an iMessage attachment file disappears from disk before the adapter can re-host it (e.g., user emptied the Messages cache), the message is still ingested, the attachment is dropped from the envelope, and the original path is preserved in `attachments_json` on the `messages` row for forensics. Logged at WARN.

## Consequences

### Positive

- **Stable URLs forever.** An agent that fetches an attachment URL minutes, hours, or days after the message arrived gets the same bytes — no expired signatures, no missing files.
- **Context replay works.** `get_message_context` (blueprint §6.1) returns surrounding messages with their attachments intact, even if those messages are weeks old.
- **Webhook receivers can dereference URLs.** A non-MCP consumer (n8n, custom script) can follow the URL with the bearer token and get the bytes from anywhere on the local network.
- **Single auth boundary.** Attachment access reuses the same bearer-token auth as the rest of the API. No "share this temporary signed URL" path to manage.
- **Cross-platform symmetry.** The agent does not have to know whether a file came from Discord or iMessage — both are served from the same endpoint with the same shape.
- **Defense against CDN URL leaks.** The Discord CDN URL never escapes the adapter's logs, so a leaked envelope cannot be used to dereference the original file from outside.

### Negative

- **Local disk footprint.** Every attachment is stored on the Mac running the adapter. At personal scale this is negligible (a few GB/year), but no eviction is implemented in v1. A retention sweeper is post-v1 work.
- **Ingest latency increases by one file copy/download per attachment.** The §6.1 SLO of P95 < 3 s receive→visible includes this work; the bounded stability run validates it (see ADR 0007).
- **The adapter must be reachable to serve attachments.** A consumer on another host cannot fetch attachments unless the adapter binds to a non-localhost interface — but that is a deployment question, not a design one.
- **Re-host is best-effort.** If Discord 5xx's the CDN GET, or the iMessage file is gone, we drop that attachment from the envelope (with WARN log + forensics in `attachments_json`). The message itself still surfaces.

### Neutral

- Storage backend is just the filesystem in v1. Swapping in an object store (S3, R2) post-v1 is a `bytes_path` semantics change with no envelope impact.

## Alternatives considered

- **Pass-through Discord CDN URLs, expose `file://` for iMessage.** Rejected — fails the "stable URL across replay" requirement on both platforms. iMessage `file://` URLs are also not dereferenceable by anything that is not running on the same Mac, which breaks remote consumers entirely.
- **Pass-through with on-demand re-fetch.** Adapter records the original URL/path; on `GET /attachments/{id}` it fetches lazily. Rejected because (a) it leaves a fragile dependency on Discord's CDN being healthy whenever an agent reads context, (b) it does not solve the iMessage "file may have been deleted from disk" case at all.
- **Re-host but expire local copies after N days.** Considered for v1; deferred. v1 retains forever; a retention sweeper is post-v1 (out-of-scope per spec §3.2).

## References

- Blueprint §3 — Normalized message envelope (`attachments[].url`, `attachments[].id`, `attachments[].size_bytes`)
- Blueprint §5.3 — `attachments` table
- Blueprint §9 — original "Attachment handling" open question (now resolved)
- Spec §5.8 / REQ-AMG-008 — Attachment re-hosting feature
- Spec §11.2 — `AMG_ATTACHMENT_DIR` env var
