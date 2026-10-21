import type {
	IDataObject,
	IExecuteFunctions,
	IHttpRequestMethods,
	IHttpRequestOptions,
	ILoadOptionsFunctions,
	IN8nHttpFullResponse,
	IPollFunctions,
	JsonObject,
} from 'n8n-workflow';
import { NodeApiError, NodeOperationError, sleep } from 'n8n-workflow';

/** The server clamps the per-call wait to this many seconds (see design doc). */
export const SERVER_WAIT_CLAMP_SECONDS = 25;

/** Default and maximum overall wait deadline exposed in the node UI. */
export const DEFAULT_WAIT_TIMEOUT_SECONDS = 60;
export const MAX_WAIT_TIMEOUT_SECONDS = 300;

/** Fallback sleep when a 429 has no Retry-After header. */
const DEFAULT_RETRY_AFTER_SECONDS = 1;

/**
 * The mailsocket API rejects `limit` outside [1, 100] (`backend/apps/api/pagination.py`).
 * Every page request must therefore stay within this bound regardless of how
 * many total items the caller ultimately wants.
 */
export const MAX_PAGE_SIZE = 100;

export interface WaitLoopOptions {
	inboxId: string;
	require: 'otp' | 'link' | 'any';
	timeoutSeconds: number;
	since?: string | number;
	minConfidence?: number;
}

export type WaitLoopOutcome =
	| { status: 'matched'; data: IDataObject }
	| { status: 'timeout' };

/**
 * Strip anything that looks like a mailsocket API key from a string. Keys
 * are `ms_live_...` / `ms_test_...` (an opaque token after the prefix) —
 * redact greedily so a partial/garbled token embedded in nested HTTP-library
 * error text is still fully scrubbed.
 */
const API_KEY_PATTERN = /ms_(?:live|test)_[A-Za-z0-9_-]+/g;

function redactSecrets(value: string): string {
	return value.replace(API_KEY_PATTERN, '[REDACTED]');
}

/**
 * Recursively redact API keys from every string field of a transport error
 * (message, description, stack, nested `response`/`cause`/`config` objects,
 * etc.) before it is ever handed to `NodeApiError`, which surfaces these
 * fields verbatim in n8n's UI and execution logs/exports. HTTP-library
 * errors (e.g. axios) can carry the request URL/headers containing the
 * credential, so this must walk the whole object graph, not just `.message`.
 */
export function redactErrorObject(error: unknown, seen = new WeakSet<object>()): unknown {
	if (typeof error === 'string') {
		return redactSecrets(error);
	}
	if (error === null || typeof error !== 'object') {
		return error;
	}
	if (seen.has(error as object)) {
		return error;
	}
	seen.add(error as object);

	if (Array.isArray(error)) {
		return error.map((item) => redactErrorObject(item, seen));
	}

	const sanitized: Record<string, unknown> = {};
	for (const [key, value] of Object.entries(error as Record<string, unknown>)) {
		sanitized[key] = redactErrorObject(value, seen);
	}
	// Error instances don't enumerate `message`/`stack`/`name` via Object.entries.
	if (error instanceof Error) {
		sanitized.message = redactSecrets(error.message);
		if (error.stack) sanitized.stack = redactSecrets(error.stack);
		sanitized.name = error.name;
	}
	return sanitized;
}

/**
 * Perform an authenticated request against the mailsocket API using the
 * node's own credential (no runtime dependency on the mailsocket SDK).
 */
export async function mailsocketApiRequest(
	this: IExecuteFunctions | ILoadOptionsFunctions | IPollFunctions,
	method: IHttpRequestMethods,
	endpoint: string,
	qs: IDataObject = {},
	body: IDataObject = {},
	options: { timeout?: number; returnFullResponse?: boolean; ignoreHttpStatusErrors?: boolean } = {},
) {
	const credentials = await this.getCredentials('mailsocketApi');
	const baseUrl = String(credentials.baseUrl || 'https://dash.mailsocket.app/api/v1').replace(
		/\/+$/,
		'',
	);
	if (!baseUrl.startsWith('https://')) {
		throw new NodeOperationError(this.getNode(), 'The mailsocket Base URL must start with https://');
	}

	const requestOptions: IHttpRequestOptions = {
		method,
		url: `${baseUrl}${endpoint}`,
		qs,
		json: true,
		returnFullResponse: options.returnFullResponse ?? false,
		ignoreHttpStatusErrors: options.ignoreHttpStatusErrors ?? false,
	};
	if (options.timeout !== undefined) {
		requestOptions.timeout = options.timeout;
	}
	if (Object.keys(body).length > 0) {
		requestOptions.body = body;
	}

	try {
		return await this.helpers.httpRequestWithAuthentication.call(
			this,
			'mailsocketApi',
			requestOptions,
		);
	} catch (error) {
		throw new NodeApiError(this.getNode(), redactErrorObject(error) as JsonObject);
	}
}

/**
 * Fetch list results from a paginated endpoint, honoring the server's
 * `limit` bound of [1, 100] on every single page request.
 *
 * - `returnAll = true`: pages through 100-row pages up to `hardCap` total.
 * - `returnAll = false`: stops as soon as `total` items have been collected
 *   (never requesting more pages than needed, never returning more than
 *   asked for), still paging in <=100-row chunks if `total` exceeds 100.
 */
export async function mailsocketApiRequestAllItems(
	this: IExecuteFunctions,
	endpoint: string,
	qs: IDataObject,
	options: { returnAll: boolean; total: number; hardCap?: number },
): Promise<IDataObject[]> {
	const hardCap = options.hardCap ?? 500;
	const collectionCap = options.returnAll ? hardCap : Math.min(options.total, hardCap);

	const results: IDataObject[] = [];
	let cursor: string | undefined;

	do {
		const remaining = collectionCap - results.length;
		const pageSize = Math.max(1, Math.min(MAX_PAGE_SIZE, remaining));
		const query: IDataObject = { ...qs, limit: pageSize };
		if (cursor) query.cursor = cursor;

		const response = (await mailsocketApiRequest.call(this, 'GET', endpoint, query)) as {
			data: IDataObject[];
			pagination?: { next_cursor?: string; has_more?: boolean };
		};
		results.push(...(response.data ?? []));
		cursor = response.pagination?.has_more ? response.pagination?.next_cursor : undefined;
	} while (cursor && results.length < collectionCap);

	return results.slice(0, collectionCap);
}

function clamp(value: number, min: number, max: number): number {
	return Math.min(max, Math.max(min, value));
}

/**
 * Poll `GET /inboxes/{id}/messages/wait` until a message matches or the
 * overall deadline elapses.
 *
 * `since` is resolved ONCE, before the loop starts, and reused on every
 * call — pinning it for the whole loop is what stops a message that lands
 * between two calls from being skipped (see design doc "Facts verified").
 * Each call is capped at `SERVER_WAIT_CLAMP_SECONDS` (the server's own
 * clamp); the last call is shortened to whatever time remains.
 */
export async function waitForMessage(
	this: IExecuteFunctions,
	opts: WaitLoopOptions,
): Promise<WaitLoopOutcome> {
	const deadline = Date.now() + clamp(opts.timeoutSeconds, 1, MAX_WAIT_TIMEOUT_SECONDS) * 1000;
	// An empty Since field (cleared in the UI, or an expression that resolved
	// to '' / whitespace) means the same as the default: '0'. The node must
	// never send `since=` — the server reads an empty value as "request start
	// time", which can miss a code that arrived before the wait began.
	const since =
		opts.since === undefined || opts.since === null || String(opts.since).trim() === ''
			? '0'
			: opts.since;

	for (;;) {
		const remainingMs = deadline - Date.now();
		if (remainingMs <= 0) {
			return { status: 'timeout' };
		}

		const perCallSeconds = clamp(remainingMs / 1000, 1, SERVER_WAIT_CLAMP_SECONDS);
		const qs: IDataObject = {
			require: opts.require,
			timeout: perCallSeconds,
			since,
		};
		if (opts.minConfidence !== undefined) {
			qs.min_confidence = opts.minConfidence;
		}

		const response = (await mailsocketApiRequest.call(
			this,
			'GET',
			`/inboxes/${opts.inboxId}/messages/wait`,
			qs,
			{},
			{
				timeout: (perCallSeconds + 10) * 1000,
				returnFullResponse: true,
				ignoreHttpStatusErrors: true,
			},
		)) as IN8nHttpFullResponse;

		const statusCode = response.statusCode;
		if (statusCode === 200) {
			const body = response.body as { data?: IDataObject };
			return { status: 'matched', data: body?.data ?? {} };
		}
		if (statusCode === 204) {
			continue;
		}
		if (statusCode === 429) {
			const headers = (response.headers ?? {}) as Record<string, string | string[] | undefined>;
			const retryAfterHeader = headers['retry-after'] ?? headers['Retry-After'];
			const retryAfterValue = Array.isArray(retryAfterHeader) ? retryAfterHeader[0] : retryAfterHeader;
			const retryAfterSeconds = retryAfterValue ? Number(retryAfterValue) : DEFAULT_RETRY_AFTER_SECONDS;
			const waitMs = Math.max(0, (Number.isFinite(retryAfterSeconds) ? retryAfterSeconds : DEFAULT_RETRY_AFTER_SECONDS) * 1000);

			if (Date.now() + waitMs > deadline) {
				return { status: 'timeout' };
			}
			await sleep(waitMs);
			continue;
		}

		// 401 / 403 / 404 / anything else: surface a clean, key-free error.
		const body = (response.body ?? {}) as { error?: { message?: string; code?: string } };
		const message = body.error?.message || `mailsocket API returned HTTP ${statusCode}`;
		throw new NodeApiError(this.getNode(), {
			message,
			httpCode: String(statusCode),
		} as JsonObject);
	}
}
