/**
 * Synchronous-await HTTP client for the mailsocket v1 REST API.
 *
 * Zero runtime dependencies — uses the global `fetch` (Node 18+). ESM.
 */

import { AuthError, MailsocketError, NotFound, RateLimited, WaitTimeout } from "./errors.js";
import type {
  ClientOptions,
  Inbox,
  InboxDetail,
  ListMessagesOptions,
  Message,
  MessageSummary,
  Page,
  RequireKind,
  TestCodeResult,
  WaitOptions,
} from "./types.js";

export const DEFAULT_BASE_URL = "https://dash.mailsocket.app/api/v1";

/** The server clamps the per-call wait to [1, 25] seconds. */
const WAIT_TIMEOUT_MAX_SECONDS = 25;
/** Client-side socket slack added above the server block, in ms. */
const WAIT_SOCKET_BUFFER_MS = 10_000;
/** Default retry delay (ms) when a 429 carries no Retry-After header. */
const DEFAULT_RETRY_AFTER_MS = 1_000;

const USER_AGENT = "mailsocket-typescript/0.3.1";
/** Response header with the cutoff a wait call actually used. */
const WAIT_SINCE_HEADER = "mailsocket-wait-since";

/**
 * Percent-encode a caller-supplied id as ONE path segment (`/`, `?`, `#`,
 * `%`, … are escaped by `encodeURIComponent`).
 *
 * An empty id and the bare dot segments `.` / `..` are rejected with a
 * `MailsocketError` before any request is made: WHATWG URL parsing (used by
 * `fetch`) normalises `.`/`..` — and their `%2E` forms — away, so they can
 * never reach the server as a single segment. No real id is ever one of these.
 */
function seg(value: string): string {
  const raw = String(value);
  if (raw === "" || raw === "." || raw === "..") {
    throw new MailsocketError(
      `Invalid id ${JSON.stringify(raw)}: ids must be non-empty and not "." or "..".`,
      { code: "invalid_id" },
    );
  }
  return encodeURIComponent(raw);
}

/** The outcome of `waitForOtp` / `waitForLink` / `wait`. */
export interface WaitResult {
  /** The full message payload. */
  message: Message;
  /** The extracted OTP, or null. */
  otp: string | null;
  /** The OTP confidence score, or null. */
  confidence: number | null;
  /** The magic link, or null. */
  magicLink: string | null;
  /** Alias for `magicLink`. */
  link: string | null;
}

export class MailsocketClient {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly requestTimeoutMs: number;
  private readonly now: () => number;

  constructor(apiKey: string, options: ClientOptions = {}) {
    if (!apiKey) {
      throw new MailsocketError("apiKey is required");
    }
    this.apiKey = apiKey;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.requestTimeoutMs = Math.ceil(options.requestTimeoutMs ?? 30_000);
    // performance.now() is monotonic (immune to wall-clock/NTP steps) and
    // already in ms, matching Python's time.monotonic(). Date.now() would let a
    // clock adjustment mid-wait corrupt the deadline arithmetic.
    this.now = options.now ?? (() => performance.now());
  }

  // -- inboxes --------------------------------------------------------------

  async createInbox(label?: string): Promise<Inbox> {
    const body = label === undefined ? undefined : { label };
    const payload = await this.request<Inbox>("POST", "/inboxes", { json: body });
    return payload.data;
  }

  async listInboxes(limit?: number, cursor?: string): Promise<Page<Inbox>> {
    const payload = await this.request<Inbox[]>("GET", "/inboxes", {
      params: this.pageParams(limit, cursor),
    });
    return this.asPage(payload);
  }

  async getInbox(inboxId: string): Promise<InboxDetail> {
    const payload = await this.request<InboxDetail>("GET", `/inboxes/${seg(inboxId)}`);
    return payload.data;
  }

  async deleteInbox(inboxId: string): Promise<void> {
    await this.request<unknown>("DELETE", `/inboxes/${seg(inboxId)}`);
  }

  // -- messages -------------------------------------------------------------

  async listMessages(
    inboxId: string,
    options: ListMessagesOptions = {},
  ): Promise<Page<MessageSummary>> {
    const params: Record<string, string> = {};
    if (options.limit !== undefined) params["limit"] = String(options.limit);
    if (options.cursor !== undefined) params["cursor"] = options.cursor;
    if (options.hasOtp !== undefined) params["has_otp"] = options.hasOtp ? "true" : "false";
    if (options.subjectContains) params["subject_contains"] = options.subjectContains;
    if (options.sender) params["from"] = options.sender; // the API names this filter `from`
    const payload = await this.request<MessageSummary[]>("GET", `/inboxes/${seg(inboxId)}/messages`, {
      params,
    });
    return this.asPage(payload);
  }

  async getLatest(inboxId: string): Promise<Message> {
    const payload = await this.request<Message>("GET", `/inboxes/${seg(inboxId)}/messages/latest`);
    return payload.data;
  }

  async getMessage(messageId: string): Promise<Message> {
    const payload = await this.request<Message>("GET", `/messages/${seg(messageId)}`);
    return payload.data;
  }

  /**
   * Put one sample OTP email (a random 6-digit code) into `inboxId`
   * (`POST /inboxes/{id}/test-code`). Resolves with `{ message_id, sent_at }`.
   *
   * The message does NOT go through the mail server: it is stored and parsed
   * like real inbound mail, so a pending or later wait (with a `since` from
   * before this call) returns it. Shares the dashboard "Send a test code"
   * quota (5 per inbox, 20 per account per hour).
   *
   * Throws `NotFound` for an unknown, deleted or foreign inbox, `RateLimited`
   * (with `retryAfter`) over the quota, and `MailsocketError` with
   * `code: "inbox_disabled"` (HTTP 409) for a disabled inbox.
   */
  async sendTestCode(inboxId: string): Promise<TestCodeResult> {
    const payload = await this.request<TestCodeResult>("POST", `/inboxes/${seg(inboxId)}/test-code`);
    return payload.data;
  }

  // -- the moat -------------------------------------------------------------

  /** Wait for an OTP. Resolves with a `WaitResult`; `result.otp` is the code. */
  waitForOtp(inboxId: string, options: WaitOptions = {}): Promise<WaitResult> {
    return this.waitFor(inboxId, "otp", options);
  }

  /** Wait for a magic link. Resolves with a `WaitResult`; `result.link` is the URL. */
  waitForLink(inboxId: string, options: WaitOptions = {}): Promise<WaitResult> {
    return this.waitFor(inboxId, "link", options);
  }

  /** Wait for either an OTP or a magic link. */
  wait(inboxId: string, options: WaitOptions = {}): Promise<WaitResult> {
    return this.waitFor(inboxId, "any", options);
  }

  // -- internals ------------------------------------------------------------

  private async waitFor(
    inboxId: string,
    require: RequireKind,
    options: WaitOptions,
  ): Promise<WaitResult> {
    // Validate/encode the id up front so a bad id fails before any request
    // (and even when the timeout is already exhausted).
    const path = `/inboxes/${seg(inboxId)}/messages/wait`;
    const timeoutMs = options.timeout ?? 60_000;
    const deadline = this.now() + timeoutMs;
    // No caller `since`: the first call omits it (server default = the
    // request start), and every later call sends back the cutoff the previous
    // response reported, so a message that lands between two calls is still
    // matched. An older server sends no header; then each call keeps omitting
    // `since` exactly as before.
    let cursor: string | undefined =
      options.since !== undefined ? String(options.since) : undefined;

    for (;;) {
      const remainingMs = deadline - this.now();
      if (remainingMs <= 0) {
        throw new WaitTimeout(`No matching message arrived within ${timeoutMs}ms.`, {
          code: "wait_timeout",
        });
      }
      const serverTimeoutSeconds = Math.max(
        1,
        Math.min(WAIT_TIMEOUT_MAX_SECONDS, remainingMs / 1000),
      );
      const params: Record<string, string> = {
        timeout: String(serverTimeoutSeconds),
        require,
      };
      // Default omits `since` entirely so the server uses the request start
      // time: a code that arrives during this call is matched, a stale one
      // already sitting in a reused inbox is not. Pass since=0 explicitly to
      // restore the old behaviour (match anything already in the inbox).
      if (cursor !== undefined) {
        params["since"] = cursor;
      }
      if (require !== "link" && options.minConfidence !== undefined) {
        params["min_confidence"] = String(options.minConfidence);
      }

      const { status, headers, body } = await this.fetchHttp(
        "GET",
        path,
        { params },
        // AbortSignal.timeout() needs an integer (a fractional remaining
        // budget threw ERR_OUT_OF_RANGE before any network I/O).
        Math.ceil(serverTimeoutSeconds * 1000) + WAIT_SOCKET_BUFFER_MS,
      );
      const envelope = this.parseJson(body);

      if (status === 200) {
        return this.toWaitResult((envelope as { data: Message }).data);
      }
      if (status === 204) {
        // Caller's own since wins; else the server-reported cutoff of this
        // call; else unchanged (an older server sends no header).
        if (options.since === undefined) cursor = headers.get(WAIT_SINCE_HEADER) || cursor;
        continue; // the server already blocked; re-call immediately
      }
      if (status === 429) {
        const retryAfterMs = this.retryAfterMs(headers);
        const sleepMs = retryAfterMs ?? DEFAULT_RETRY_AFTER_MS;
        if (this.now() + sleepMs > deadline) {
          throw new WaitTimeout(`No matching message arrived within ${timeoutMs}ms.`, {
            code: "wait_timeout",
          });
        }
        await sleep(sleepMs);
        continue;
      }
      this.raiseForStatus(status, headers, envelope);
    }
  }

  private toWaitResult(message: Message): WaitResult {
    return {
      message,
      otp: message.otp ?? null,
      confidence: message.otp_confidence ?? null,
      magicLink: message.magic_link ?? null,
      link: message.magic_link ?? null,
    };
  }

  private async request<T>(
    method: string,
    path: string,
    opts: { params?: Record<string, string>; json?: unknown } = {},
  ): Promise<{ data: T; pagination?: import("./types.js").Pagination }> {
    const { status, headers, body } = await this.fetchHttp(method, path, opts);
    const envelope = this.parseJson(body);
    if (status >= 200 && status < 300) {
      return envelope as { data: T; pagination?: import("./types.js").Pagination };
    }
    this.raiseForStatus(status, headers, envelope);
    // Unreachable — raiseForStatus always throws.
    throw new MailsocketError(`HTTP ${status}`);
  }

  private async fetchHttp(
    method: string,
    path: string,
    opts: { params?: Record<string, string>; json?: unknown } = {},
    timeoutMs?: number,
  ): Promise<{ status: number; headers: Headers; body: string }> {
    let url = this.baseUrl + path;
    if (opts.params && Object.keys(opts.params).length > 0) {
      url += "?" + new URLSearchParams(opts.params).toString();
    }
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      Accept: "application/json",
      "User-Agent": USER_AGENT,
    };
    let init: RequestInit = { method, headers };
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    }
    if (timeoutMs !== undefined) {
      init.signal = AbortSignal.timeout(timeoutMs);
    } else if (this.requestTimeoutMs > 0) {
      init.signal = AbortSignal.timeout(this.requestTimeoutMs);
    }
    let response: Response;
    let text: string;
    try {
      response = await fetch(url, init);
      text = await response.text();
    } catch (err) {
      // fetch() rejects on network failures (DNS/connection) and when the
      // AbortSignal.timeout fires (socket timeout). Surface these as a clean
      // MailsocketError instead of leaking a raw DOMException/TypeError to the
      // caller — mirrors the Python SDK's URLError -> MailsocketError wrap.
      const reason =
        err instanceof Error ? `${err.name}: ${err.message}` : String(err);
      throw new MailsocketError(`Network error: ${reason}`, {
        code: "network_error",
      });
    }
    return { status: response.status, headers: response.headers, body: text };
  }

  private raiseForStatus(
    status: number,
    headers: Headers,
    envelope: unknown,
  ): never {
    let code: string | undefined;
    let message: string | undefined;
    if (envelope && typeof envelope === "object" && "error" in envelope) {
      const err = (envelope as { error?: { code?: string; message?: string } }).error;
      code = err?.code;
      message = err?.message;
    }
    if (status === 401) {
      throw new AuthError(message ?? "Authentication required.", {
        code: code ?? "authentication_required",
        status: 401,
      });
    }
    if (status === 404) {
      throw new NotFound(message ?? "Resource not found.", { code: code ?? "not_found", status: 404 });
    }
    if (status === 429) {
      throw new RateLimited(message ?? "Rate limit exceeded.", {
        code: code ?? "rate_limited",
        subcode: code,
        retryAfter: this.retryAfterSeconds(headers) ?? undefined,
      });
    }
    throw new MailsocketError(message ?? `HTTP ${status}`, { code, status });
  }

  private parseJson(body: string): unknown {
    if (!body) return {};
    try {
      return JSON.parse(body);
    } catch {
      return {};
    }
  }

  private retryAfterMs(headers: Headers): number | null {
    const seconds = this.retryAfterSeconds(headers);
    return seconds === null ? null : seconds * 1000;
  }

  private retryAfterSeconds(headers: Headers): number | null {
    const value = headers.get("retry-after");
    if (value === null) return null;
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return null;
    return seconds;
  }

  private pageParams(limit?: number, cursor?: string): Record<string, string> {
    const params: Record<string, string> = {};
    if (limit !== undefined) params["limit"] = String(limit);
    if (cursor !== undefined) params["cursor"] = cursor;
    return params;
  }

  private asPage<T>(payload: { data: T[]; pagination?: import("./types.js").Pagination }): Page<T> {
    const pagination = payload.pagination ?? { next_cursor: null, has_more: false };
    return {
      data: payload.data,
      pagination,
      nextCursor: pagination.next_cursor,
      hasMore: pagination.has_more,
    };
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
