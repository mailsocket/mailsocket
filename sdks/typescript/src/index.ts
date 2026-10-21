/**
 * Official TypeScript SDK for the mailsocket v1 REST API.
 */

export { MailsocketClient, DEFAULT_BASE_URL } from "./client.js";
export type { WaitResult } from "./client.js";
export {
  MailsocketError,
  AuthError,
  NotFound,
  RateLimited,
  WaitTimeout,
} from "./errors.js";
export type {
  Inbox,
  InboxDetail,
  Message,
  MessageSummary,
  MessageStatus,
  Page,
  Pagination,
  ListMessagesOptions,
  WaitOptions,
  ClientOptions,
  TestCodeResult,
} from "./types.js";
