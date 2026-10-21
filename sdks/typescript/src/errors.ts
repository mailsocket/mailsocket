/**
 * Typed errors raised by the mailsocket TypeScript SDK.
 *
 * Dual-package note: this package ships both an ESM and a CommonJS build, so an
 * app that loads it via `import` AND `require()` gets two copies of every error
 * class, and plain prototype-based `instanceof` fails across the copies. To keep
 * `err instanceof NotFound` working regardless of which copy threw, every SDK
 * error is branded (under the realm-global `Symbol.for("mailsocket.MailsocketError")`)
 * with its SDK class chain, e.g. `["NotFound", "MailsocketError"]`, and the SDK
 * classes implement `Symbol.hasInstance` to accept branded errors from either copy.
 */

/** Realm-global key, identical in the ESM and CJS copies. */
const BRAND = Symbol.for("mailsocket.MailsocketError");
/** Own static tag marking a constructor as one of the SDK's own error classes. */
const SDK_CLASS = Symbol.for("mailsocket.MailsocketError.class");

const nativeHasInstance = Function.prototype[Symbol.hasInstance];

/** The SDK class name tagged on `ctor` itself (not inherited), if any. */
function sdkClassName(ctor: unknown): string | undefined {
  if (typeof ctor !== "function" || !Object.prototype.hasOwnProperty.call(ctor, SDK_CLASS)) {
    return undefined;
  }
  return (ctor as unknown as Record<symbol, string>)[SDK_CLASS];
}

/** Base class for every error the SDK raises. */
export class MailsocketError extends Error {
  /** The server's error code (e.g. `not_found`, `rate_limited`). */
  readonly code?: string;
  /** The HTTP status code, when one triggered the error. */
  readonly status?: number;

  constructor(message: string, opts?: { code?: string; status?: number }) {
    super(message);
    this.name = new.target.name;
    this.code = opts?.code;
    this.status = opts?.status;
    // SDK class chain, most-derived first; user subclasses are skipped.
    const chain: string[] = [];
    for (let ctor: unknown = new.target; typeof ctor === "function"; ctor = Object.getPrototypeOf(ctor)) {
      const name = sdkClassName(ctor);
      if (name !== undefined) chain.push(name);
      if (ctor === MailsocketError) break;
    }
    Object.defineProperty(this, BRAND, {
      value: Object.freeze(chain),
      enumerable: false,
      writable: false,
      configurable: false,
    });
  }

  /**
   * `instanceof` that also recognises errors thrown by the package's other
   * build (ESM vs CommonJS). Ordinary prototype-chain semantics are kept; the
   * cross-copy fallback applies only to the SDK's own classes, never to user
   * subclasses.
   */
  static [Symbol.hasInstance]<T>(
    this: abstract new (...args: never[]) => T,
    instance: unknown,
  ): instance is T {
    if (nativeHasInstance.call(this, instance)) return true;
    const name = sdkClassName(this);
    if (name === undefined || !(instance instanceof Error)) return false;
    const chain = (instance as unknown as Record<symbol, unknown>)[BRAND];
    return Array.isArray(chain) && chain.includes(name) && chain[chain.length - 1] === "MailsocketError";
  }
}

/** The API key is missing or invalid (HTTP 401). */
export class AuthError extends MailsocketError {}

/** The requested resource does not exist or is not owned (HTTP 404). */
export class NotFound extends MailsocketError {}

/**
 * A rate limit was hit (HTTP 429).
 *
 * Carries `subcode` (the server error code: `rate_limited`,
 * `too_many_wait_requests` or `wait_capacity`) and `retryAfter` (seconds to
 * wait, from the `Retry-After` header, or `null`).
 */
export class RateLimited extends MailsocketError {
  readonly subcode?: string;
  /** Seconds to wait before retrying, from the `Retry-After` header. */
  readonly retryAfter?: number;

  constructor(
    message: string,
    opts?: { code?: string; subcode?: string; retryAfter?: number },
  ) {
    super(message, { code: opts?.code, status: 429 });
    this.subcode = opts?.subcode;
    this.retryAfter = opts?.retryAfter;
  }
}

/** No matching message arrived before the overall wait deadline. */
export class WaitTimeout extends MailsocketError {}

// Tag the SDK's own classes with explicit (minifier-proof) names.
for (const [ctor, name] of [
  [MailsocketError, "MailsocketError"],
  [AuthError, "AuthError"],
  [NotFound, "NotFound"],
  [RateLimited, "RateLimited"],
  [WaitTimeout, "WaitTimeout"],
] as const) {
  Object.defineProperty(ctor, SDK_CLASS, { value: name, enumerable: false, writable: false, configurable: false });
}
