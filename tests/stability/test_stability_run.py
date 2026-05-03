"""Bounded automated stability run (task #73; spec §3.2 / §6.1).

This is the v1 build-acceptance gate. The test boots the full AMC stack
against the Phase 0 fakes, drives mixed Discord + iMessage inbound load
for a configurable window, polls /messages/unread + /messages/mark_read
from 5 in-process agents in parallel, and asserts the spec's stability
SLOs:

* P95 receive→visible < 3s
* P95 send→ack       < 2s
* ≥ 95 % first-attempt webhook success
* Zero unhandled exceptions in adapter logs
* Cursor monotonically increases
* 5 concurrent agents read independently

CI duration default
-------------------
The default duration is **60 SECONDS** so the test fits inside a normal
CI run. The spec's 30-minute window is the operator-run gate, not the
CI gate. A maintainer can override via the ``AMC_STABILITY_DURATION_SECONDS``
env var (capped at 3600s).

Operator override:
    AMC_STABILITY_DURATION_SECONDS=1800 uv run pytest \\
        tests/stability/test_stability_run.py -v --override-ini=addopts=

The test writes a ``stability-metrics.json`` file under ``tmp_path`` and
asserts against it so a failure surfaces both the assertion *and* the
captured numbers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.stability.harness import (
    DURATION_CEILING_SECONDS,
    StabilityConfig,
    StabilityHarness,
)

# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

# Default CI duration. The spec calls for 30 min but that's the operator-run
# gate; the CI test must complete in well under 5 min total wall time.
DEFAULT_DURATION_SECONDS: float = 60.0

# How many /messages/send probes to issue during the run. We don't drive a
# fixed rate for sends — one probe every 5s gives us enough samples for a
# meaningful P50/P95 over a 60s run while keeping the rate-limit headroom
# generous.
SEND_PROBE_INTERVAL_SECONDS: float = 5.0

# SLO thresholds (spec §3.2 / §6.1).
P95_RECEIVE_TO_VISIBLE_BUDGET_MS: float = 3_000.0
P95_SEND_TO_ACK_BUDGET_MS: float = 2_000.0
WEBHOOK_FIRST_ATTEMPT_SUCCESS_FLOOR: float = 0.95


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_duration() -> float:
    """Return the CI duration, honoring ``AMC_STABILITY_DURATION_SECONDS``.

    Caps the value at :data:`DURATION_CEILING_SECONDS` so an env override
    can't blow past the 1-hour ad-hoc ceiling from the task brief.
    """
    raw = os.environ.get("AMC_STABILITY_DURATION_SECONDS", "").strip()
    if not raw:
        return DEFAULT_DURATION_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise pytest.UsageError(
            f"AMC_STABILITY_DURATION_SECONDS must be numeric; got {raw!r}"
        ) from exc
    if value <= 0:
        raise pytest.UsageError(f"AMC_STABILITY_DURATION_SECONDS must be > 0; got {value}")
    return min(value, float(DURATION_CEILING_SECONDS))


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_bounded_stability_run_meets_slos(tmp_path: Path) -> None:
    """Boot full stack, drive mixed load, assert spec §3.2 / §6.1 SLOs.

    Walks the full lifecycle:

    1. Materialize a writable chat.db copy + a fresh AMC SQLite DB.
    2. Boot FakeDiscordGateway, in-process webhook receiver (with 5%
       500 flake), real Discord+iMessage connectors, real WebhookWorker,
       FastAPI app via TestClient.
    3. Inject 1 msg/s of mixed Discord + iMessage inbound for the
       configured duration. 5 agents poll /messages/unread + mark_read
       in parallel from in-process clients.
    4. Issue periodic /messages/send probes to capture send→ack latency.
    5. Wait for the webhook delivery queue to drain (bounded).
    6. Collect a JSON metrics summary, write it to tmp_path, and assert
       each SLO with the captured numbers in the failure message.
    """
    duration_seconds = _resolve_duration()
    config = StabilityConfig(
        duration_seconds=duration_seconds,
        target_rate_per_second=1.0,
        webhook_failure_rate=0.05,
        parallel_agents=5,
        rng_seed=42,
    )

    metrics_path = tmp_path / "stability-metrics.json"

    async with StabilityHarness(config, tmp_dir=tmp_path) as harness:
        # Run inbound traffic + agent poll loops in the background; in
        # parallel, fire a /messages/send probe every
        # SEND_PROBE_INTERVAL_SECONDS so we capture real send-path latency
        # alongside the inbound stream.
        traffic_task = __import__("asyncio").create_task(
            harness.drive_traffic(),
            name="stability.drive_traffic",
        )

        # Block until the first inbound message has been visible to at
        # least one agent before issuing send probes — the channels row is
        # populated by the inbound sink, so /messages/send would 404
        # CHANNEL_NOT_FOUND otherwise.
        await _wait_for_channel_seeded(harness, timeout=10.0)

        # Issue a steady stream of send probes for the duration of the run.
        await _drive_send_probes(
            harness,
            duration=duration_seconds,
            interval=SEND_PROBE_INTERVAL_SECONDS,
        )

        # Wait for the inbound traffic generator to exit cleanly.
        await traffic_task

        # Bound the webhook-drain wait so a runaway test still terminates.
        await harness.wait_for_pending_webhooks(timeout=15.0)

        metrics = await harness.collect_metrics()
        harness.write_metrics_json(metrics_path, metrics)

        # Surface the captured error records inline if anything goes wrong.
        error_messages = [r.getMessage() for r in harness.error_records()]

    # ------------------------------------------------------------------
    # Re-load metrics from the JSON file we just wrote so the assertions
    # exercise the on-disk artifact (the task brief asks for both).
    # ------------------------------------------------------------------
    on_disk = json.loads(metrics_path.read_text())

    # Sanity: every captured field made the round trip.
    for field_name in (
        "p50_receive_to_visible_ms",
        "p95_receive_to_visible_ms",
        "p50_send_to_ack_ms",
        "p95_send_to_ack_ms",
        "webhook_first_attempt_success_rate",
        "error_log_count",
        "cursor_monotonic",
        "distinct_agents_with_reads",
    ):
        assert field_name in on_disk, f"metrics JSON missing field: {field_name}"

    # ------------------------------------------------------------------
    # Acceptance assertions (spec §3.2 / §6.1).
    # ------------------------------------------------------------------
    failures: list[str] = []

    # 1) Inbound visibility — we should have observed at least one message
    #    end-to-end so the latency percentiles are meaningful.
    if on_disk["inbound_visible_count"] == 0:
        failures.append(
            f"no inbound messages were visible during the run "
            f"(injected={on_disk['inbound_count']}); "
            f"system never ingested anything"
        )

    # 2) P95 receive→visible < 3s
    p95_rtv = on_disk["p95_receive_to_visible_ms"]
    if on_disk["inbound_visible_count"] > 0 and p95_rtv >= P95_RECEIVE_TO_VISIBLE_BUDGET_MS:
        failures.append(
            f"P95 receive→visible {p95_rtv:.1f}ms exceeds "
            f"{P95_RECEIVE_TO_VISIBLE_BUDGET_MS:.0f}ms budget"
        )

    # 3) P95 send→ack < 2s. Skip the assertion if we somehow captured no
    #    send samples — a 0 score would always pass and silently mask a
    #    regression in send-probe scheduling.
    p95_sa = on_disk["p95_send_to_ack_ms"]
    send_count = len(harness._send_ack_latencies_ms)  # noqa: SLF001
    if send_count == 0:
        failures.append("no /messages/send probes were captured during the run")
    elif p95_sa >= P95_SEND_TO_ACK_BUDGET_MS:
        failures.append(
            f"P95 send→ack {p95_sa:.1f}ms exceeds {P95_SEND_TO_ACK_BUDGET_MS:.0f}ms budget"
        )

    # 4) ≥ 95 % first-attempt webhook success.
    success_rate = on_disk["webhook_first_attempt_success_rate"]
    if on_disk["webhook_attempts"] > 0 and success_rate < WEBHOOK_FIRST_ATTEMPT_SUCCESS_FLOOR:
        failures.append(
            f"webhook first-attempt success rate {success_rate:.3f} "
            f"below {WEBHOOK_FIRST_ATTEMPT_SUCCESS_FLOOR:.2f} floor "
            f"(attempts={on_disk['webhook_attempts']}, "
            f"500_count={on_disk['webhook_500_count']})"
        )

    # 5) Zero unhandled exceptions in adapter logs.
    if on_disk["error_log_count"] > 0:
        joined = "\n  - ".join(error_messages[:10])
        failures.append(
            f"adapter logged {on_disk['error_log_count']} ERROR-level "
            f"records; first up to 10:\n  - {joined}"
        )

    # 6) Cursor monotonically increases.
    if not on_disk["cursor_monotonic"]:
        failures.append("connector_state.cursor regressed during the run")

    # 7) 5 concurrent agents each read at least one message — proves the
    #    per-agent read state isn't accidentally shared.
    if on_disk["distinct_agents_with_reads"] != on_disk["parallel_agents"]:
        failures.append(
            f"only {on_disk['distinct_agents_with_reads']} of "
            f"{on_disk['parallel_agents']} agents observed any messages"
        )

    if failures:
        joined = "\n  - ".join(failures)
        # Embed the full metrics dict so the failure output carries the
        # numbers without relying on ``-v`` test output.
        raise AssertionError(
            "Stability run did not meet spec §3.2 / §6.1 SLOs:\n  - "
            f"{joined}\n\nCaptured metrics:\n{json.dumps(on_disk, indent=2, sort_keys=True)}"
        )


# ---------------------------------------------------------------------------
# Local async helpers
# ---------------------------------------------------------------------------


async def _wait_for_channel_seeded(
    harness: StabilityHarness,
    *,
    timeout: float,
) -> None:
    """Wait until at least one inbound message has been observed by an agent.

    The /messages/send route 404s on a channel it has never seen inbound;
    we wait for the first inbound to drain through the agent poll loop so
    the channels row exists before issuing send probes.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if harness._inbound_visible_at:  # noqa: SLF001
            return
        await asyncio.sleep(0.1)
    # Don't raise — the assertion later will catch the "no inbound visible"
    # case and surface a clearer error.


async def _drive_send_probes(
    harness: StabilityHarness,
    *,
    duration: float,
    interval: float,
) -> None:
    """Issue periodic ``POST /messages/send`` probes for ``duration`` seconds."""
    import asyncio

    end = asyncio.get_running_loop().time() + duration
    sent = 0
    while asyncio.get_running_loop().time() < end:
        sent += 1
        await harness.issue_send_probe(text_body=f"send probe #{sent}")
        # Sleep but don't overshoot the deadline.
        remaining = end - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(interval, remaining))
