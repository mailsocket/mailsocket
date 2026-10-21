import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { IExecuteFunctions, INode } from 'n8n-workflow';
import { NodeApiError, NodeOperationError } from 'n8n-workflow';

import {
	SERVER_WAIT_CLAMP_SECONDS,
	waitForMessage,
	mailsocketApiRequest,
	redactErrorObject,
} from '../nodes/Mailsocket/GenericFunctions';

const FAKE_NODE: INode = {
	id: '1',
	name: 'mailsocket',
	type: 'n8n-nodes-mailsocket.mailsocket',
	typeVersion: 1,
	position: [0, 0],
	parameters: {},
};

const FAKE_API_KEY = 'ms_live_secret_token_value';

/** Build a minimal IExecuteFunctions-like context with a mocked httpRequestWithAuthentication. */
function buildContext(httpRequestWithAuthentication: ReturnType<typeof vi.fn>) {
	return {
		getNode: () => FAKE_NODE,
		getCredentials: vi.fn().mockResolvedValue({
			apiKey: FAKE_API_KEY,
			baseUrl: 'https://dash.mailsocket.app/api/v1',
		}),
		helpers: {
			httpRequestWithAuthentication,
		},
	} as unknown as IExecuteFunctions;
}

describe('waitForMessage', () => {
	beforeEach(() => {
		vi.useRealTimers();
	});

	it('returns the OTP after 204, 204, 200 and pins `since` across every call', async () => {
		const calls: Array<Record<string, unknown>> = [];
		const httpRequestWithAuthentication = vi
			.fn()
			.mockImplementationOnce((_ctx, opts) => {
				calls.push(opts.qs);
				return Promise.resolve({ statusCode: 204, headers: {}, body: {} });
			})
			.mockImplementationOnce((_ctx, opts) => {
				calls.push(opts.qs);
				return Promise.resolve({ statusCode: 204, headers: {}, body: {} });
			})
			.mockImplementationOnce((_ctx, opts) => {
				calls.push(opts.qs);
				return Promise.resolve({
					statusCode: 200,
					headers: {},
					body: { data: { otp: '123456', otp_confidence: 0.9, id: 'msg_1' } },
				});
			});

		const ctx = buildContext(httpRequestWithAuthentication);
		const outcome = await waitForMessage.call(ctx, {
			inboxId: 'inbox_1',
			require: 'otp',
			timeoutSeconds: 60,
			since: 'msg_abc',
		});

		expect(outcome.status).toBe('matched');
		if (outcome.status === 'matched') {
			expect(outcome.data.otp).toBe('123456');
		}
		expect(calls).toHaveLength(3);
		// `since` is identical on every call.
		const sinceValues = new Set(calls.map((c) => c.since));
		expect(sinceValues.size).toBe(1);
		expect([...sinceValues][0]).toBe('msg_abc');
		// Every per-call timeout stays within the server clamp.
		for (const call of calls) {
			expect(call.timeout as number).toBeLessThanOrEqual(SERVER_WAIT_CLAMP_SECONDS);
		}
	});

	it.each([
		['undefined', undefined],
		['empty string', ''],
		['whitespace', ' 	 '],
	])('resolves a %s since to "0" (never sends since=)', async (_label, since) => {
		const calls: Array<Record<string, unknown>> = [];
		const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
			calls.push(opts.qs);
			return Promise.resolve({ statusCode: 200, headers: {}, body: { data: { otp: '1', id: 'msg_1' } } });
		});
		const ctx = buildContext(httpRequestWithAuthentication);
		await waitForMessage.call(ctx, {
			inboxId: 'inbox_1',
			require: 'otp',
			timeoutSeconds: 10,
			since: since as string | undefined,
		});
		expect(calls[0].since).toBe('0');
	});

	it('keeps an explicit since of 0 (number) and a message id as given', async () => {
		const calls: Array<Record<string, unknown>> = [];
		const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
			calls.push(opts.qs);
			return Promise.resolve({ statusCode: 200, headers: {}, body: { data: { otp: '1', id: 'msg_1' } } });
		});
		const ctx = buildContext(httpRequestWithAuthentication);
		await waitForMessage.call(ctx, { inboxId: 'i', require: 'otp', timeoutSeconds: 10, since: 0 });
		await waitForMessage.call(ctx, { inboxId: 'i', require: 'otp', timeoutSeconds: 10, since: 'msg_9' });
		expect(calls.map((c) => c.since)).toEqual([0, 'msg_9']);
	});

	it('shortens the last call to the remaining time near the deadline', async () => {
		const calls: Array<Record<string, unknown>> = [];
		let now = 0;
		vi.spyOn(Date, 'now').mockImplementation(() => now);

		const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
			calls.push(opts.qs);
			now += (opts.qs.timeout as number) * 1000; // simulate the call taking its full timeout
			if (calls.length < 2) {
				return Promise.resolve({ statusCode: 204, headers: {}, body: {} });
			}
			return Promise.resolve({
				statusCode: 200,
				headers: {},
				body: { data: { otp: '000000' } },
			});
		});

		const ctx = buildContext(httpRequestWithAuthentication);
		// 30s deadline: first call is clamped to 25s, second call gets the
		// remaining ~5s.
		const outcome = await waitForMessage.call(ctx, {
			inboxId: 'inbox_1',
			require: 'otp',
			timeoutSeconds: 30,
		});

		expect(outcome.status).toBe('matched');
		expect(calls[0].timeout).toBe(SERVER_WAIT_CLAMP_SECONDS);
		expect(calls[1].timeout as number).toBeLessThan(SERVER_WAIT_CLAMP_SECONDS);

		vi.restoreAllMocks();
	});

	it('honors Retry-After on 429 then succeeds', async () => {
		vi.useFakeTimers();
		const httpRequestWithAuthentication = vi
			.fn()
			.mockImplementationOnce(() =>
				Promise.resolve({ statusCode: 429, headers: { 'retry-after': '2' }, body: {} }),
			)
			.mockImplementationOnce(() =>
				Promise.resolve({
					statusCode: 200,
					headers: {},
					body: { data: { magic_link: 'https://example.com/verify' } },
				}),
			);

		const ctx = buildContext(httpRequestWithAuthentication);
		const promise = waitForMessage.call(ctx, {
			inboxId: 'inbox_1',
			require: 'link',
			timeoutSeconds: 60,
		});

		await vi.advanceTimersByTimeAsync(2000);
		const outcome = await promise;

		expect(outcome.status).toBe('matched');
		if (outcome.status === 'matched') {
			expect(outcome.data.magic_link).toBe('https://example.com/verify');
		}
		vi.useRealTimers();
	});

	it('gives a timeout outcome (no oversleep) when Retry-After would exceed the deadline', async () => {
		vi.useFakeTimers();
		const httpRequestWithAuthentication = vi
			.fn()
			.mockResolvedValue({ statusCode: 429, headers: { 'retry-after': '999' }, body: {} });

		const ctx = buildContext(httpRequestWithAuthentication);
		const outcome = await waitForMessage.call(ctx, {
			inboxId: 'inbox_1',
			require: 'otp',
			timeoutSeconds: 5,
		});

		expect(outcome.status).toBe('timeout');
		// Only the first call happens; no sleep call is scheduled past the deadline.
		expect(httpRequestWithAuthentication).toHaveBeenCalledTimes(1);
		vi.useRealTimers();
	});

	it('times out cleanly when the deadline elapses with only 204s', async () => {
		let now = 0;
		vi.spyOn(Date, 'now').mockImplementation(() => now);
		const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
			now += (opts.qs.timeout as number) * 1000;
			return Promise.resolve({ statusCode: 204, headers: {}, body: {} });
		});

		const ctx = buildContext(httpRequestWithAuthentication);
		const outcome = await waitForMessage.call(ctx, {
			inboxId: 'inbox_1',
			require: 'otp',
			timeoutSeconds: 10,
		});

		expect(outcome.status).toBe('timeout');
		vi.restoreAllMocks();
	});

	it('maps 401/403/404 to a NodeApiError whose message never contains the API key', async () => {
		const httpRequestWithAuthentication = vi.fn().mockResolvedValue({
			statusCode: 401,
			headers: {},
			body: { error: { message: 'Authentication required.', code: 'authentication_required' } },
		});

		const ctx = buildContext(httpRequestWithAuthentication);
		await expect(
			waitForMessage.call(ctx, { inboxId: 'inbox_1', require: 'otp', timeoutSeconds: 10 }),
		).rejects.toBeInstanceOf(NodeApiError);

		try {
			await waitForMessage.call(ctx, { inboxId: 'inbox_1', require: 'otp', timeoutSeconds: 10 });
			throw new Error('expected waitForMessage to reject');
		} catch (error) {
			const err = error as NodeApiError;
			expect(String(err.message)).not.toContain(FAKE_API_KEY);
			expect(String(err.description ?? '')).not.toContain(FAKE_API_KEY);
			expect(JSON.stringify(err)).not.toContain(FAKE_API_KEY);
		}
	});
});

describe('redactErrorObject', () => {
	it('redacts a key embedded in a plain string', () => {
		const result = redactErrorObject(`socket hang up ${FAKE_API_KEY}`);
		expect(result).not.toContain(FAKE_API_KEY);
	});

	it('redacts a key from every string field of a nested error object, including message/description/stack', () => {
		const error = {
			message: `request failed: ${FAKE_API_KEY}`,
			description: `Authorization: Bearer ${FAKE_API_KEY}`,
			response: {
				config: { url: `https://dash.mailsocket.app/api/v1/inboxes?key=${FAKE_API_KEY}` },
				headers: { authorization: `Bearer ${FAKE_API_KEY}` },
			},
		};

		const sanitized = redactErrorObject(error);
		const serialized = JSON.stringify(sanitized);
		expect(serialized).not.toContain(FAKE_API_KEY);
	});

	it('redacts message and stack on a real Error instance', () => {
		const error = new Error(`boom ${FAKE_API_KEY}`);
		const sanitized = redactErrorObject(error) as { message: string; stack?: string };
		expect(sanitized.message).not.toContain(FAKE_API_KEY);
		if (sanitized.stack) {
			expect(sanitized.stack).not.toContain(FAKE_API_KEY);
		}
	});
});

describe('mailsocketApiRequest', () => {
	it('rejects a non-https base URL before making any network call', async () => {
		const httpRequestWithAuthentication = vi.fn();
		const ctx = {
			getNode: () => FAKE_NODE,
			getCredentials: vi
				.fn()
				.mockResolvedValue({ apiKey: FAKE_API_KEY, baseUrl: 'http://insecure.example.com' }),
			helpers: { httpRequestWithAuthentication },
		} as unknown as IExecuteFunctions;

		await expect(mailsocketApiRequest.call(ctx, 'GET', '/inboxes')).rejects.toBeInstanceOf(
			NodeOperationError,
		);
		expect(httpRequestWithAuthentication).not.toHaveBeenCalled();
	});

	it('wraps a transport failure in NodeApiError with the full fake key absent from message, description, and the serialized error', async () => {
		const httpRequestWithAuthentication = vi
			.fn()
			.mockRejectedValue(new Error(`socket hang up ${FAKE_API_KEY}`));
		const ctx = buildContext(httpRequestWithAuthentication);

		await expect(mailsocketApiRequest.call(ctx, 'GET', '/inboxes')).rejects.toBeInstanceOf(
			NodeApiError,
		);

		try {
			await mailsocketApiRequest.call(ctx, 'GET', '/inboxes');
			throw new Error('expected mailsocketApiRequest to reject');
		} catch (error) {
			const err = error as NodeApiError;
			expect(String(err.message)).not.toContain(FAKE_API_KEY);
			expect(String(err.description ?? '')).not.toContain(FAKE_API_KEY);
			expect(JSON.stringify(err)).not.toContain(FAKE_API_KEY);
		}
	});

	it('wraps a transport failure whose key is nested in response/config fields without leaking it', async () => {
		const nestedError = Object.assign(new Error('Request failed with status code 401'), {
			response: {
				config: { url: `https://dash.mailsocket.app/api/v1/inboxes?key=${FAKE_API_KEY}` },
				headers: { authorization: `Bearer ${FAKE_API_KEY}` },
			},
		});
		const httpRequestWithAuthentication = vi.fn().mockRejectedValue(nestedError);
		const ctx = buildContext(httpRequestWithAuthentication);

		try {
			await mailsocketApiRequest.call(ctx, 'GET', '/inboxes');
			throw new Error('expected mailsocketApiRequest to reject');
		} catch (error) {
			expect(JSON.stringify(error)).not.toContain(FAKE_API_KEY);
		}
	});
});
