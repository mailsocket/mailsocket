import { describe, it, expect, vi } from 'vitest';
import type { IExecuteFunctions, INode, ICredentialTestFunctions } from 'n8n-workflow';

import { Mailsocket } from '../nodes/Mailsocket/Mailsocket.node';
import { MailsocketApi, mailsocketCredentialTest } from '../credentials/MailsocketApi.credentials';

const FAKE_NODE: INode = {
	id: '1',
	name: 'mailsocket',
	type: 'n8n-nodes-mailsocket.mailsocket',
	typeVersion: 1,
	position: [0, 0],
	parameters: {},
};

interface Scenario {
	resource: string;
	operation: string;
	params: Record<string, unknown>;
	itemCount?: number;
}

function buildContext(
	scenario: Scenario,
	httpRequestWithAuthentication: ReturnType<typeof vi.fn>,
) {
	const itemCount = scenario.itemCount ?? 1;
	return {
		getNode: () => FAKE_NODE,
		getInputData: () => Array.from({ length: itemCount }, () => ({ json: {} })),
		getNodeParameter: (name: string, _itemIndex?: number, fallback?: unknown) => {
			if (name === 'resource') return scenario.resource;
			if (name === 'operation') return scenario.operation;
			if (name in scenario.params) return scenario.params[name];
			return fallback;
		},
		continueOnFail: () => false,
		getCredentials: vi.fn().mockResolvedValue({
			apiKey: 'ms_live_secret_token_value',
			baseUrl: 'https://dash.mailsocket.app/api/v1',
		}),
		helpers: { httpRequestWithAuthentication },
	} as unknown as IExecuteFunctions;
}

type IDataObjectQs = Record<string, unknown>;

describe('Mailsocket node execute()', () => {
	it('creates an inbox: asserts method/path/body and returned shape', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi
			.fn()
			.mockResolvedValue({ data: { id: 'inbox_1', address: 'a@mailsocket.app' } });
		const ctx = buildContext(
			{ resource: 'inbox', operation: 'create', params: { label: 'signup test' } },
			httpRequestWithAuthentication,
		);

		const result = await node.execute.call(ctx);

		expect(httpRequestWithAuthentication).toHaveBeenCalledTimes(1);
		const [, opts] = httpRequestWithAuthentication.mock.calls[0];
		expect(opts.method).toBe('POST');
		expect(opts.url).toBe('https://dash.mailsocket.app/api/v1/inboxes');
		expect(opts.body).toEqual({ label: 'signup test' });

		expect(result[0]).toHaveLength(1);
		expect(result[0][0].json).toMatchObject({ id: 'inbox_1', address: 'a@mailsocket.app' });
		expect(result[0][0].pairedItem).toEqual({ item: 0 });
	});

	it('deletes an inbox: asserts method/path and returned shape', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockResolvedValue({});
		const ctx = buildContext(
			{ resource: 'inbox', operation: 'delete', params: { inboxId: 'inbox_9' } },
			httpRequestWithAuthentication,
		);

		const result = await node.execute.call(ctx);

		const [, opts] = httpRequestWithAuthentication.mock.calls[0];
		expect(opts.method).toBe('DELETE');
		expect(opts.url).toBe('https://dash.mailsocket.app/api/v1/inboxes/inbox_9');
		expect(result[0][0].json).toEqual({ deleted: 'inbox_9' });
	});

	it('sends a test code: POST /inboxes/{id}/test-code with no body, returns message_id/sent_at', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi
			.fn()
			.mockResolvedValue({ data: { message_id: 'msg_t1', sent_at: '2026-10-08T12:00:00Z' } });
		const ctx = buildContext(
			{ resource: 'inbox', operation: 'sendTestCode', params: { inboxId: 'inbox_7' } },
			httpRequestWithAuthentication,
		);

		const result = await node.execute.call(ctx);

		expect(httpRequestWithAuthentication).toHaveBeenCalledTimes(1);
		const [credName, opts] = httpRequestWithAuthentication.mock.calls[0];
		expect(credName).toBe('mailsocketApi');
		expect(opts.method).toBe('POST');
		expect(opts.url).toBe('https://dash.mailsocket.app/api/v1/inboxes/inbox_7/test-code');
		expect(opts.body).toBeUndefined();
		expect(result[0][0].json).toEqual({ message_id: 'msg_t1', sent_at: '2026-10-08T12:00:00Z' });
		expect(result[0][0].pairedItem).toEqual({ item: 0 });
	});

	it('Send Test Code surfaces a 429 as a NodeApiError without the key', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockRejectedValue(
			Object.assign(new Error('Too many test codes. Try again at 13:00 UTC.'), {
				httpCode: '429',
				config: { headers: { Authorization: 'Bearer ms_live_secret_token_value' } },
			}),
		);
		const ctx = buildContext(
			{ resource: 'inbox', operation: 'sendTestCode', params: { inboxId: 'inbox_7' } },
			httpRequestWithAuthentication,
		);

		let caught: unknown;
		try {
			await node.execute.call(ctx);
		} catch (error) {
			caught = error;
		}
		expect(caught).toBeDefined();
		expect(JSON.stringify(caught)).not.toContain('ms_live_secret_token_value');
		expect(String((caught as Error).message)).not.toContain('ms_live_secret_token_value');
	});

	it('exposes Send Test Code on the Inbox resource with its own Inbox ID field', () => {
		const node = new Mailsocket();
		const op = node.description.properties.find(
			(p) => p.name === 'operation' && p.displayOptions?.show?.resource?.includes('inbox'),
		);
		const values = (op?.options as Array<{ value: string }>).map((o) => o.value);
		expect(values).toContain('sendTestCode');
		const field = node.description.properties.find(
			(p) =>
				p.name === 'inboxId' &&
				p.displayOptions?.show?.resource?.includes('inbox') &&
				(p.displayOptions?.show?.operation as string[] | undefined)?.includes('sendTestCode'),
		);
		expect(field?.required).toBe(true);
	});

	it.each([
		['empty string', ''],
		['whitespace', '   '],
	])('Wait for OTP with an %s Since sends since=0, never since=', async (_label, since) => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockResolvedValue({
			statusCode: 200,
			headers: {},
			body: { data: { otp: '111111', id: 'msg_1' } },
		});
		const ctx = buildContext(
			{
				resource: 'message',
				operation: 'waitForOtp',
				params: { inboxId: 'inbox_1', timeoutSeconds: 10, since },
			},
			httpRequestWithAuthentication,
		);

		await node.execute.call(ctx);

		const [, opts] = httpRequestWithAuthentication.mock.calls[0];
		expect(opts.qs.since).toBe('0');
	});

	it('returns an empty item when Get Latest hits a 404 with the default option', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockRejectedValue(
			Object.assign(new Error('Not Found'), { httpCode: '404' }),
		);
		const ctx = buildContext(
			{
				resource: 'message',
				operation: 'getLatest',
				params: { inboxId: 'inbox_1', onEmpty: 'returnEmpty' },
			},
			httpRequestWithAuthentication,
		);

		const result = await node.execute.call(ctx);

		expect(result[0][0].json).toEqual({ found: false });
	});

	it('throws on Get Latest 404 when onEmpty is "error"', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockRejectedValue(
			Object.assign(new Error('Not Found'), { httpCode: '404' }),
		);
		const ctx = buildContext(
			{
				resource: 'message',
				operation: 'getLatest',
				params: { inboxId: 'inbox_1', onEmpty: 'error' },
			},
			httpRequestWithAuthentication,
		);

		await expect(node.execute.call(ctx)).rejects.toThrow();
	});

	it('projects Wait for OTP output to otp/confidence/subject/from/message_id/received_at', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockResolvedValue({
			statusCode: 200,
			headers: {},
			body: {
				data: {
					otp: '654321',
					otp_confidence: 0.8,
					subject: 'Your code',
					from: 'noreply@example.com',
					id: 'msg_5',
					received_at: '2024-01-01T00:00:00Z',
					magic_link: 'https://example.com/should-not-appear',
				},
			},
		});
		const ctx = buildContext(
			{
				resource: 'message',
				operation: 'waitForOtp',
				params: { inboxId: 'inbox_1', timeoutSeconds: 10 },
			},
			httpRequestWithAuthentication,
		);

		const result = await node.execute.call(ctx);

		expect(result[0][0].json).toEqual({
			otp: '654321',
			confidence: 0.8,
			subject: 'Your code',
			from: 'noreply@example.com',
			message_id: 'msg_5',
			received_at: '2024-01-01T00:00:00Z',
		});
	});

	it('projects Wait for Link output to magic_link/subject/from/message_id', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockResolvedValue({
			statusCode: 200,
			headers: {},
			body: {
				data: {
					magic_link: 'https://example.com/verify',
					subject: 'Verify',
					from: 'noreply@example.com',
					id: 'msg_6',
					otp: 'should-not-appear',
				},
			},
		});
		const ctx = buildContext(
			{
				resource: 'message',
				operation: 'waitForLink',
				params: { inboxId: 'inbox_1', timeoutSeconds: 10 },
			},
			httpRequestWithAuthentication,
		);

		const result = await node.execute.call(ctx);

		expect(result[0][0].json).toEqual({
			magic_link: 'https://example.com/verify',
			subject: 'Verify',
			from: 'noreply@example.com',
			message_id: 'msg_6',
		});
	});

	it('Wait timeout with onTimeout="returnEmpty" returns a timed_out item instead of throwing', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockResolvedValue({
			statusCode: 204,
			headers: {},
			body: {},
		});
		const ctx = buildContext(
			{
				resource: 'message',
				operation: 'waitForOtp',
				params: { inboxId: 'inbox_1', timeoutSeconds: 1, onTimeout: 'returnEmpty' },
			},
			httpRequestWithAuthentication,
		);

		const result = await node.execute.call(ctx);

		expect(result[0][0].json).toEqual({ timed_out: true });
	});

	it('Wait timeout with onTimeout="error" (default) throws NodeOperationError', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi.fn().mockResolvedValue({
			statusCode: 204,
			headers: {},
			body: {},
		});
		const ctx = buildContext(
			{
				resource: 'message',
				operation: 'waitForOtp',
				params: { inboxId: 'inbox_1', timeoutSeconds: 1 },
			},
			httpRequestWithAuthentication,
		);

		await expect(node.execute.call(ctx)).rejects.toThrow(/No matching message arrived/);
	});

	it('continueOnFail pushes an error item instead of throwing', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi
			.fn()
			.mockRejectedValue(new Error('boom'));
		const ctx = {
			...buildContext(
				{ resource: 'inbox', operation: 'create', params: {} },
				httpRequestWithAuthentication,
			),
			continueOnFail: () => true,
		} as unknown as IExecuteFunctions;

		const result = await node.execute.call(ctx);

		expect(result[0]).toHaveLength(1);
		expect(result[0][0].json).toHaveProperty('error');
		expect(result[0][0].pairedItem).toEqual({ item: 0 });
	});

	it('sets pairedItem for every output item across multiple input items', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi
			.fn()
			.mockResolvedValue({ data: { id: 'inbox_x' } });
		const ctx = buildContext(
			{ resource: 'inbox', operation: 'create', params: {}, itemCount: 3 },
			httpRequestWithAuthentication,
		);

		const result = await node.execute.call(ctx);

		expect(result[0]).toHaveLength(3);
		result[0].forEach((item, i) => {
			expect(item.pairedItem).toEqual({ item: i });
		});
	});

	describe('Inbox List pagination', () => {
		it('non-Return-All with limit=50 sends limit<=100 and returns exactly 50', async () => {
			const node = new Mailsocket();
			let callCount = 0;
			const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
				callCount += 1;
				expect(opts.qs.limit).toBeLessThanOrEqual(100);
				return Promise.resolve({
					data: Array.from({ length: opts.qs.limit }, (_, i) => ({ id: `inbox_${i}` })),
					pagination: { has_more: false },
				});
			});
			const ctx = buildContext(
				{ resource: 'inbox', operation: 'list', params: { returnAll: false, limit: 50 } },
				httpRequestWithAuthentication,
			);

			const result = await node.execute.call(ctx);

			expect(callCount).toBe(1);
			expect(result[0]).toHaveLength(50);
		});

		it('non-Return-All with limit=101 pages in <=100 chunks, propagates cursor, returns exactly 101', async () => {
			const node = new Mailsocket();
			const calls: IDataObjectQs[] = [];
			const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
				calls.push(opts.qs);
				if (!opts.qs.cursor) {
					expect(opts.qs.limit).toBeLessThanOrEqual(100);
					return Promise.resolve({
						data: Array.from({ length: 100 }, (_, i) => ({ id: `inbox_${i}` })),
						pagination: { has_more: true, next_cursor: 'cursor_page2' },
					});
				}
				expect(opts.qs.cursor).toBe('cursor_page2');
				expect(opts.qs.limit).toBeLessThanOrEqual(100);
				return Promise.resolve({
					data: [{ id: 'inbox_100' }],
					pagination: { has_more: false },
				});
			});
			const ctx = buildContext(
				{ resource: 'inbox', operation: 'list', params: { returnAll: false, limit: 101 } },
				httpRequestWithAuthentication,
			);

			const result = await node.execute.call(ctx);

			expect(calls).toHaveLength(2);
			expect(result[0]).toHaveLength(101);
		});

		it('Return All pages through 100-row pages up to the 500 hard cap', async () => {
			const node = new Mailsocket();
			let callCount = 0;
			const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
				callCount += 1;
				expect(opts.qs.limit).toBeLessThanOrEqual(100);
				return Promise.resolve({
					data: Array.from({ length: 100 }, (_, i) => ({ id: `inbox_${callCount}_${i}` })),
					pagination: { has_more: true, next_cursor: `cursor_${callCount}` },
				});
			});
			const ctx = buildContext(
				{ resource: 'inbox', operation: 'list', params: { returnAll: true, limit: 50 } },
				httpRequestWithAuthentication,
			);

			const result = await node.execute.call(ctx);

			// 500 hard cap / 100 per page = 5 calls, exactly 500 rows.
			expect(callCount).toBe(5);
			expect(result[0]).toHaveLength(500);
		});
	});

	describe('Message List pagination', () => {
		it('non-Return-All with limit=50 returns exactly 50 in one call', async () => {
			const node = new Mailsocket();
			const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
				expect(opts.qs.limit).toBeLessThanOrEqual(100);
				return Promise.resolve({
					data: Array.from({ length: opts.qs.limit }, (_, i) => ({ id: `msg_${i}` })),
					pagination: { has_more: false },
				});
			});
			const ctx = buildContext(
				{
					resource: 'message',
					operation: 'list',
					params: { inboxId: 'inbox_1', returnAll: false, limit: 50 },
				},
				httpRequestWithAuthentication,
			);

			const result = await node.execute.call(ctx);

			expect(httpRequestWithAuthentication).toHaveBeenCalledTimes(1);
			expect(result[0]).toHaveLength(50);
		});

		it('non-Return-All with limit=101 returns exactly 101 across paged <=100 calls', async () => {
			const node = new Mailsocket();
			const calls: IDataObjectQs[] = [];
			const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
				calls.push(opts.qs);
				expect(opts.qs.limit).toBeLessThanOrEqual(100);
				if (!opts.qs.cursor) {
					return Promise.resolve({
						data: Array.from({ length: 100 }, (_, i) => ({ id: `msg_${i}` })),
						pagination: { has_more: true, next_cursor: 'cursor_2' },
					});
				}
				expect(opts.qs.cursor).toBe('cursor_2');
				return Promise.resolve({ data: [{ id: 'msg_100' }], pagination: { has_more: false } });
			});
			const ctx = buildContext(
				{
					resource: 'message',
					operation: 'list',
					params: { inboxId: 'inbox_1', returnAll: false, limit: 101 },
				},
				httpRequestWithAuthentication,
			);

			const result = await node.execute.call(ctx);

			expect(calls).toHaveLength(2);
			expect(result[0]).toHaveLength(101);
		});

		it('Return All returns exactly the 500-row hard cap when more is available', async () => {
			const node = new Mailsocket();
			let callCount = 0;
			const httpRequestWithAuthentication = vi.fn().mockImplementation((_ctx, opts) => {
				callCount += 1;
				expect(opts.qs.limit).toBeLessThanOrEqual(100);
				return Promise.resolve({
					data: Array.from({ length: 100 }, (_, i) => ({ id: `msg_${callCount}_${i}` })),
					pagination: { has_more: true, next_cursor: `cursor_${callCount}` },
				});
			});
			const ctx = buildContext(
				{
					resource: 'message',
					operation: 'list',
					params: { inboxId: 'inbox_1', returnAll: true, limit: 50 },
				},
				httpRequestWithAuthentication,
			);

			const result = await node.execute.call(ctx);

			expect(callCount).toBe(5);
			expect(result[0]).toHaveLength(500);
		});
	});

	it('never leaks the API key through a continueOnFail error item', async () => {
		const node = new Mailsocket();
		const httpRequestWithAuthentication = vi
			.fn()
			.mockRejectedValue(new Error('socket hang up ms_live_secret_token_value'));
		const ctx = {
			...buildContext(
				{ resource: 'inbox', operation: 'create', params: {} },
				httpRequestWithAuthentication,
			),
			continueOnFail: () => true,
		} as unknown as IExecuteFunctions;

		const result = await node.execute.call(ctx);

		const serialized = JSON.stringify(result[0][0].json);
		expect(serialized).not.toContain('ms_live_secret_token_value');
	});
});

describe('MailsocketApi credential', () => {
	it('declares a credential test against GET /inboxes?limit=1', () => {
		const credential = new MailsocketApi();
		expect(credential.test.request.url).toBe('/inboxes');
		expect(credential.test.request.qs).toEqual({ limit: 1 });
	});

	it('sends the API key as a Bearer Authorization header, never in a query string', () => {
		const credential = new MailsocketApi();
		const headers = (credential.authenticate as { properties: { headers: Record<string, string> } })
			.properties.headers;
		expect(headers.Authorization).toContain('Bearer');
		expect(JSON.stringify(credential.test)).not.toContain('apiKey=');
	});
});

describe('mailsocketCredentialTest', () => {
	function buildTestCtx(httpRequest: ReturnType<typeof vi.fn>): ICredentialTestFunctions {
		return { helpers: { httpRequest } } as unknown as ICredentialTestFunctions;
	}

	it('maps a 401 to "Invalid API key"', async () => {
		const httpRequest = vi.fn().mockResolvedValue({ statusCode: 401 });
		const ctx = buildTestCtx(httpRequest);

		const result = await mailsocketCredentialTest.call(
			ctx,
			{ data: { apiKey: 'ms_live_x', baseUrl: 'https://dash.mailsocket.app/api/v1' } } as never,
		);

		expect(result).toEqual({ status: 'Error', message: 'Invalid API key' });
	});

	it('maps a 403 to an account-verification message', async () => {
		const httpRequest = vi.fn().mockResolvedValue({ statusCode: 403 });
		const ctx = buildTestCtx(httpRequest);

		const result = await mailsocketCredentialTest.call(
			ctx,
			{ data: { apiKey: 'ms_live_x', baseUrl: 'https://dash.mailsocket.app/api/v1' } } as never,
		);

		expect(result.status).toBe('Error');
		expect(result.message).toMatch(/verify your account email/i);
	});

	it('rejects a plain-http base URL without making a network call', async () => {
		const httpRequest = vi.fn();
		const ctx = buildTestCtx(httpRequest);

		const result = await mailsocketCredentialTest.call(
			ctx,
			{ data: { apiKey: 'ms_live_x', baseUrl: 'http://insecure.example.com' } } as never,
		);

		expect(result.status).toBe('Error');
		expect(result.message).toMatch(/https:\/\//);
		expect(httpRequest).not.toHaveBeenCalled();
	});

	it('reports success on a 200 response', async () => {
		const httpRequest = vi.fn().mockResolvedValue({ statusCode: 200 });
		const ctx = buildTestCtx(httpRequest);

		const result = await mailsocketCredentialTest.call(
			ctx,
			{ data: { apiKey: 'ms_live_secret_token_value', baseUrl: 'https://dash.mailsocket.app/api/v1' } } as never,
		);

		expect(result.status).toBe('OK');
	});

	it('redacts the API key from a rejected transport error, reusing the shared sanitizer', async () => {
		const fakeKey = 'ms_live_secret_token_value';
		const httpRequest = vi
			.fn()
			.mockRejectedValue(new Error(`request failed Authorization: Bearer ${fakeKey}`));
		const ctx = buildTestCtx(httpRequest);

		const result = await mailsocketCredentialTest.call(
			ctx,
			{ data: { apiKey: fakeKey, baseUrl: 'https://dash.mailsocket.app/api/v1' } } as never,
		);

		expect(result.status).toBe('Error');
		expect(result.message).not.toContain(fakeKey);
		expect(JSON.stringify(result)).not.toContain(fakeKey);
	});
});
