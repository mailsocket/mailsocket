import type {
	IAuthenticateGeneric,
	ICredentialTestFunctions,
	ICredentialTestRequest,
	ICredentialType,
	ICredentialsDecrypted,
	INodeCredentialTestResult,
	INodeProperties,
} from 'n8n-workflow';

import { redactErrorObject } from '../nodes/Mailsocket/GenericFunctions';

export class MailsocketApi implements ICredentialType {
	name = 'mailsocketApi';

	displayName = 'Mailsocket API';

	documentationUrl = 'https://dash.mailsocket.app/docs/#sdks';

	icon = { light: 'file:mailsocket.svg', dark: 'file:mailsocket.dark.svg' } as const;

	properties: INodeProperties[] = [
		{
			displayName: 'API Key',
			name: 'apiKey',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			required: true,
			description: 'Your mailsocket API key (starts with ms_live_). Create one in the dashboard.',
		},
		{
			displayName: 'Base URL',
			name: 'baseUrl',
			type: 'string',
			default: 'https://dash.mailsocket.app/api/v1',
			description: 'The mailsocket API base URL. Must use https://.',
		},
	];

	authenticate: IAuthenticateGeneric = {
		type: 'generic',
		properties: {
			headers: {
				Authorization: '=Bearer {{$credentials.apiKey}}',
			},
		},
	};

	/**
	 * Declarative fallback kept for tooling/UIs that only understand
	 * `ICredentialType.test` (e.g. static analyzers). The node itself uses
	 * the richer `mailsocketCredentialTest` function below — registered via
	 * `testedBy` on the node's credential entry — which additionally
	 * enforces HTTPS-only and maps 401/403 to actionable messages, matching
	 * `mailsocketApiRequest`'s own validation instead of bypassing it.
	 */
	test: ICredentialTestRequest = {
		request: {
			baseURL: '={{$credentials.baseUrl}}',
			url: '/inboxes',
			qs: { limit: 1 },
		},
	};
}

export async function mailsocketCredentialTest(
	this: ICredentialTestFunctions,
	credential: ICredentialsDecrypted,
): Promise<INodeCredentialTestResult> {
	const data = credential.data as { apiKey?: string; baseUrl?: string };
	const baseUrl = String(data.baseUrl || 'https://dash.mailsocket.app/api/v1').replace(/\/+$/, '');

	// Same rule the node enforces at execution time (`mailsocketApiRequest`):
	// a credential must not be able to "test successfully" over plain HTTP
	// only to be rejected later when the node actually runs.
	if (!baseUrl.startsWith('https://')) {
		return {
			status: 'Error',
			message: 'The mailsocket Base URL must start with https://',
		};
	}

	// `ICredentialTestFunctions.helpers` only types the deprecated `request`
	// helper; the runtime also exposes `httpRequest`, which is what node
	// execution itself uses (`mailsocketApiRequest`) — reuse it here so the
	// test path never diverges from the execution path.
	const helpers = this.helpers as unknown as {
		httpRequest: (options: {
			method: string;
			url: string;
			qs?: Record<string, unknown>;
			json?: boolean;
			headers?: Record<string, string>;
			returnFullResponse?: boolean;
			ignoreHttpStatusErrors?: boolean;
		}) => Promise<{ statusCode?: number }>;
	};

	try {
		const response = await helpers.httpRequest({
			method: 'GET',
			url: `${baseUrl}/inboxes`,
			qs: { limit: 1 },
			json: true,
			headers: { Authorization: `Bearer ${data.apiKey ?? ''}` },
			returnFullResponse: true,
			ignoreHttpStatusErrors: true,
		});

		if (response.statusCode === 401) {
			return { status: 'Error', message: 'Invalid API key' };
		}
		if (response.statusCode === 403) {
			return {
				status: 'Error',
				message: 'Account not permitted to use the API — verify your account email and try again',
			};
		}
		if (response.statusCode !== undefined && response.statusCode >= 400) {
			return {
				status: 'Error',
				message: `mailsocket API returned HTTP ${response.statusCode}`,
			};
		}
		return { status: 'OK', message: 'Connection successful' };
	} catch (error) {
		// Reuse the same sanitizer the execution path uses (`mailsocketApiRequest`)
		// instead of a second redaction policy: transport errors (e.g. axios) can
		// carry the credential in `.message`/nested `response`/`config` fields.
		const sanitized = redactErrorObject(error) as { message?: string };
		return {
			status: 'Error',
			message: sanitized.message || 'Could not connect to the mailsocket API',
		};
	}
}
