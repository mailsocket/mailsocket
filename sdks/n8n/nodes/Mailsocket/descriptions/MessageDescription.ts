import type { INodeProperties } from 'n8n-workflow';

export const messageOperations: INodeProperties[] = [
	{
		displayName: 'Operation',
		name: 'operation',
		type: 'options',
		noDataExpression: true,
		displayOptions: { show: { resource: ['message'] } },
		options: [
			{
				name: 'Wait for OTP',
				value: 'waitForOtp',
				description: 'Block until a one-time passcode arrives',
				action: 'Wait for an OTP',
			},
			{
				name: 'Wait for Link',
				value: 'waitForLink',
				description: 'Block until a magic link arrives',
				action: 'Wait for a magic link',
			},
			{
				name: 'Get Latest',
				value: 'getLatest',
				description: 'Get the most recent message in an inbox',
				action: 'Get the latest message',
			},
			{
				name: 'List',
				value: 'list',
				description: 'List messages in an inbox',
				action: 'List messages',
			},
		],
		default: 'waitForOtp',
	},
];

const inboxIdField: INodeProperties = {
	displayName: 'Inbox ID',
	name: 'inboxId',
	type: 'string',
	default: '',
	required: true,
	description: 'The inbox to operate on',
};

const sinceField: INodeProperties = {
	displayName: 'Since',
	name: 'since',
	type: 'string',
	default: '0',
	description:
		'Only match messages received after this point: "0" for any message already in the inbox, an ISO 8601 timestamp, a Unix timestamp, or a "msg_..." cursor from a previous message in the same inbox. Pinned once for the whole wait loop so no message is skipped between calls.',
};

const timeoutField: INodeProperties = {
	displayName: 'Timeout (Seconds)',
	name: 'timeoutSeconds',
	type: 'number',
	default: 60,
	typeOptions: { minValue: 1, maxValue: 300 },
	description:
		'How long to wait overall before giving up. The server itself only ever holds one call open for up to 25s; the node re-calls automatically until this deadline. For waits longer than 5 minutes, use the mailsocket Trigger node instead.',
};

const onTimeoutField: INodeProperties = {
	displayName: 'On Timeout',
	name: 'onTimeout',
	type: 'options',
	options: [
		{ name: 'Error', value: 'error' },
		{ name: 'Return Empty Item', value: 'returnEmpty' },
	],
	default: 'error',
	description: 'What to do when no matching message arrives before the timeout',
};

export const messageFields: INodeProperties[] = [
	{ ...inboxIdField, displayOptions: { show: { resource: ['message'], operation: ['waitForOtp'] } } },
	{
		displayName: 'Minimum Confidence',
		name: 'minConfidence',
		type: 'number',
		default: 0,
		typeOptions: { minValue: 0, maxValue: 1, numberStepSize: 0.1 },
		description: 'Only match OTPs the parser is at least this confident about (0-1)',
		displayOptions: { show: { resource: ['message'], operation: ['waitForOtp'] } },
	},
	{ ...sinceField, displayOptions: { show: { resource: ['message'], operation: ['waitForOtp'] } } },
	{ ...timeoutField, displayOptions: { show: { resource: ['message'], operation: ['waitForOtp'] } } },
	{ ...onTimeoutField, displayOptions: { show: { resource: ['message'], operation: ['waitForOtp'] } } },

	{ ...inboxIdField, displayOptions: { show: { resource: ['message'], operation: ['waitForLink'] } } },
	{ ...sinceField, displayOptions: { show: { resource: ['message'], operation: ['waitForLink'] } } },
	{ ...timeoutField, displayOptions: { show: { resource: ['message'], operation: ['waitForLink'] } } },
	{ ...onTimeoutField, displayOptions: { show: { resource: ['message'], operation: ['waitForLink'] } } },

	{ ...inboxIdField, displayOptions: { show: { resource: ['message'], operation: ['getLatest'] } } },
	{
		displayName: 'When No Message Exists',
		name: 'onEmpty',
		type: 'options',
		options: [
			{ name: 'Return Empty Item', value: 'returnEmpty' },
			{ name: 'Error', value: 'error' },
		],
		default: 'returnEmpty',
		description: 'What to do when the inbox has no messages yet',
		displayOptions: { show: { resource: ['message'], operation: ['getLatest'] } },
	},

	{ ...inboxIdField, displayOptions: { show: { resource: ['message'], operation: ['list'] } } },
	{
		displayName: 'Has OTP',
		name: 'hasOtp',
		type: 'boolean',
		default: false,
		description: 'Whether to only return messages that contain an OTP',
		displayOptions: { show: { resource: ['message'], operation: ['list'] } },
	},
	{
		displayName: 'Subject Contains',
		name: 'subjectContains',
		type: 'string',
		default: '',
		displayOptions: { show: { resource: ['message'], operation: ['list'] } },
	},
	{
		displayName: 'From',
		name: 'from',
		type: 'string',
		default: '',
		description: 'Only return messages from this sender',
		displayOptions: { show: { resource: ['message'], operation: ['list'] } },
	},
	{
		displayName: 'Return All',
		name: 'returnAll',
		type: 'boolean',
		default: false,
		description: 'Whether to return all results or only up to a given limit',
		displayOptions: { show: { resource: ['message'], operation: ['list'] } },
	},
	{
		displayName: 'Limit',
		name: 'limit',
		type: 'number',
		default: 50,
		typeOptions: { minValue: 1, maxValue: 500 },
		description: 'Max number of results to return',
		displayOptions: {
			show: { resource: ['message'], operation: ['list'], returnAll: [false] },
		},
	},
];
