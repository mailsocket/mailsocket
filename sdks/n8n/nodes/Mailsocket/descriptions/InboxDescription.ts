import type { INodeProperties } from 'n8n-workflow';

export const inboxOperations: INodeProperties[] = [
	{
		displayName: 'Operation',
		name: 'operation',
		type: 'options',
		noDataExpression: true,
		displayOptions: { show: { resource: ['inbox'] } },
		options: [
			{
				name: 'Create',
				value: 'create',
				description: 'Create a new throwaway inbox',
				action: 'Create an inbox',
			},
			{
				name: 'Delete',
				value: 'delete',
				description: 'Soft-delete an inbox',
				action: 'Delete an inbox',
			},
			{
				name: 'List',
				value: 'list',
				description: 'List your inboxes',
				action: 'List inboxes',
			},
			{
				name: 'Send Test Code',
				value: 'sendTestCode',
				description:
					'Put one sample OTP email into the inbox (no mail server) to try a Wait for OTP without real email',
				action: 'Send a test code to an inbox',
			},
		],
		default: 'create',
	},
];

export const inboxFields: INodeProperties[] = [
	{
		displayName: 'Label',
		name: 'label',
		type: 'string',
		default: '',
		description: 'Optional label to identify this inbox',
		displayOptions: { show: { resource: ['inbox'], operation: ['create'] } },
	},
	{
		displayName: 'Inbox ID',
		name: 'inboxId',
		type: 'string',
		default: '',
		required: true,
		description: 'The ID of the inbox to delete',
		displayOptions: { show: { resource: ['inbox'], operation: ['delete'] } },
	},
	{
		displayName: 'Inbox ID',
		name: 'inboxId',
		type: 'string',
		default: '',
		required: true,
		description:
			'The inbox to send the sample code to. The code is stored straight away, so a Wait for OTP that runs after this node needs Since set to a cutoff from before it (for example "0" on a fresh inbox, or the previous message ID). Limited to 5 per inbox and 20 per account per hour.',
		displayOptions: { show: { resource: ['inbox'], operation: ['sendTestCode'] } },
	},
	{
		displayName: 'Return All',
		name: 'returnAll',
		type: 'boolean',
		default: false,
		description: 'Whether to return all results or only up to a given limit',
		displayOptions: { show: { resource: ['inbox'], operation: ['list'] } },
	},
	{
		displayName: 'Limit',
		name: 'limit',
		type: 'number',
		default: 50,
		typeOptions: { minValue: 1, maxValue: 500 },
		description: 'Max number of results to return',
		displayOptions: {
			show: { resource: ['inbox'], operation: ['list'], returnAll: [false] },
		},
	},
];
