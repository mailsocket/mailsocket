/**
 * Wire types for the mailsocket v1 REST API.
 *
 * Field names match the server JSON exactly — note `from` and `otp_confidence`
 * are snake_case / reserved-word keys in the payload.
 */

export interface Inbox {
  id: string;
  label: string;
  address: string;
  is_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface InboxDetail extends Inbox {
  message_count_month: number;
  webhook_configured: boolean;
}

export type MessageStatus = "received" | "parsing" | "parsed" | "failed";

export interface MessageSummary {
  id: string;
  inbox_id: string;
  from: string;
  to: string[];
  cc: string[];
  subject: string;
  received_at: string;
  status: MessageStatus;
  otp: string | null;
  otp_confidence: number | null;
  magic_link: string | null;
  links: string[] | null;
  detected_otp: string | null;
  has_html: boolean;
}

export interface Message {
  id: string;
  inbox_id: string;
  from: string;
  to: string[];
  cc: string[];
  subject: string;
  received_at: string;
  status: MessageStatus;
  otp: string | null;
  otp_confidence: number | null;
  magic_link: string | null;
  links: string[] | null;
  detected_otp: string | null;
  envelope_from: string;
  envelope_recipient: string;
  text: string;
  html: string;
}

export interface Pagination {
  next_cursor: string | null;
  has_more: boolean;
}

/** A page of results plus pagination metadata. */
export interface Page<T> {
  data: T[];
  pagination: Pagination;
  nextCursor: string | null;
  hasMore: boolean;
}

export interface ListMessagesOptions {
  hasOtp?: boolean;
  subjectContains?: string;
  sender?: string;
  limit?: number;
  cursor?: string;
}

export interface WaitOptions {
  /** Overall client deadline in milliseconds (default 60_000). */
  timeout?: number;
  /** Minimum `otp_confidence` for an OTP match (default 0). */
  minConfidence?: number;
  /** Only messages strictly after this point match. Default: omitted, so the
   * server uses the request start time (nothing already in the inbox
   * matches); later calls in the same wait send the cutoff the server
   * reported (`Mailsocket-Wait-Since`), so nothing landing between calls is
   * missed. Pass 0 to match messages already in the inbox. */
  since?: string | number;
}

/** Result of `sendTestCode`. */
export interface TestCodeResult {
  /** Public id of the new message (`msg_...`). */
  message_id: string;
  /** When the message was stored (its `received_at`, ISO 8601). */
  sent_at: string;
}

export interface ClientOptions {
  baseUrl?: string;
  /** Socket timeout for non-wait requests, in ms (default 30_000; 0 = no timeout). */
  requestTimeoutMs?: number;
  /** Clock used for wait deadlines (ms since epoch). Injectable for tests. */
  now?: () => number;
}

export type RequireKind = "otp" | "link" | "any";

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    fields?: Record<string, unknown>;
  };
}

interface DataEnvelope<T> {
  data: T;
  pagination?: Pagination;
  error?: ErrorEnvelope["error"];
}

/** Alias for the discriminated internal envelope union. */
export type ApiEnvelope = DataEnvelope<unknown> | ErrorEnvelope;
