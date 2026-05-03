// MCP `send_message` tool — thin wrapper over `POST /messages/send`.
//
// Spec references:
//   - §6.1 (`send_message` input/output)
//   - §5.1 (POST /messages/send endpoint)
//   - §7.4.7 (Idempotency-Key header on POST /messages/send)
//   - §7.4.12 (error envelope, surfaced via http.ts)
//
// Design contract:
//   * Input shape mirrors `SendMessageRequest` in `amc/core/envelope.py`:
//     `channel_id` and `text` are required, `reply_to` and `attachments` are
//     optional, and each attachment must specify exactly one of `url` or `path`.
//   * Generates a fresh `Idempotency-Key` per call via `randomIdempotencyKey()`
//     (UUID v4). The wrapper itself owns key generation so the four-tool
//     surface stays self-contained — callers/agents do not pass keys.
//   * Returns `{ message_id, sent_at }` on success. Errors are returned as the
//     http.ts `HttpErr` discriminant (no throwing) so the per-tool error
//     mapper added in #64 can translate them uniformly.

import { z } from 'zod';

import { type HttpClient, type HttpResult, randomIdempotencyKey } from '../http.js';

// Each attachment must supply exactly one of `url` or `path`. We use a refine
// rather than a discriminated union so the public schema stays a flat object
// (matches the Python `SendAttachmentInput` shape and the spec §6.1 example).
const attachmentSchema = z
  .object({
    url: z.string().optional(),
    path: z.string().optional(),
  })
  .refine((a) => (a.url == null) !== (a.path == null), {
    message: 'Provide exactly one of url or path',
  });

const inputSchema = z.object({
  channel_id: z.string(),
  text: z.string(),
  reply_to: z.string().optional(),
  attachments: z.array(attachmentSchema).optional(),
});

export type SendMessageInput = z.infer<typeof inputSchema>;

/** Adapter response shape for `POST /messages/send` (spec §6.1). */
export interface SendMessageOutput {
  readonly message_id: string;
  readonly sent_at: string;
}

export const sendMessage = {
  name: 'send_message' as const,
  description:
    'Send a message to a channel. Optional `reply_to` threads under another ' +
    'message in the same channel. Each attachment must supply exactly one of ' +
    '`url` (HTTP(S)) or `path` (local file).',
  inputSchema,
  async handler(
    input: SendMessageInput,
    http: HttpClient,
  ): Promise<HttpResult<SendMessageOutput>> {
    const parsed = inputSchema.parse(input);

    const idempotencyKey = randomIdempotencyKey();

    return http.post<SendMessageOutput>('/messages/send', parsed, {
      'Idempotency-Key': idempotencyKey,
    });
  },
};
