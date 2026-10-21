import type {
	IDataObject,
	IExecuteFunctions,
	INodeExecutionData,
	INodeType,
	INodeTypeDescription,
	JsonObject,
} from 'n8n-workflow';
import { NodeApiError, NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';

import { inboxFields, inboxOperations } from './descriptions/InboxDescription';
import { messageFields, messageOperations } from './descriptions/MessageDescription';
import {
	DEFAULT_WAIT_TIMEOUT_SECONDS,
	mailsocketApiRequest,
	mailsocketApiRequestAllItems,
	waitForMessage,
} from './GenericFunctions';
import { mailsocketCredentialTest } from '../../credentials/MailsocketApi.credentials';

export class Mailsocket implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'mailsocket',
		name: 'mailsocket',
		icon: { light: 'file:mailsocket.svg', dark: 'file:mailsocket.dark.svg' },
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["resource"] + ": " + $parameter["operation"]}}',
		description: 'Create throwaway inboxes and block on the OTP or magic link',
		defaults: {
			name: 'mailsocket',
		},
		inputs: [NodeConnectionTypes.Main],
		outputs: [NodeConnectionTypes.Main],
		usableAsTool: true,
		credentials: [
			{
				name: 'mailsocketApi',
				required: true,
				testedBy: 'mailsocketCredentialTest',
			},
		],
		properties: [
			{
				displayName: 'Resource',
				name: 'resource',
				type: 'options',
				noDataExpression: true,
				options: [
					{ name: 'Inbox', value: 'inbox' },
					{ name: 'Message', value: 'message' },
				],
				default: 'message',
			},
			...inboxOperations,
			...inboxFields,
			...messageOperations,
			...messageFields,
		],
	};

	methods = {
		credentialTest: {
			mailsocketCredentialTest,
		},
	};

	async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
		const items = this.getInputData();
		const returnData: INodeExecutionData[] = [];
		const resource = this.getNodeParameter('resource', 0) as string;
		const operation = this.getNodeParameter('operation', 0) as string;

		for (let itemIndex = 0; itemIndex < items.length; itemIndex++) {
			try {
				let responseData: IDataObject | IDataObject[] = {};

				if (resource === 'inbox') {
					if (operation === 'create') {
						const label = this.getNodeParameter('label', itemIndex, '') as string;
						const body: IDataObject = label ? { label } : {};
						const result = (await mailsocketApiRequest.call(
							this,
							'POST',
							'/inboxes',
							{},
							body,
						)) as { data: IDataObject };
						responseData = result.data;
					} else if (operation === 'delete') {
						const inboxId = this.getNodeParameter('inboxId', itemIndex) as string;
						await mailsocketApiRequest.call(this, 'DELETE', `/inboxes/${inboxId}`);
						responseData = { deleted: inboxId };
					} else if (operation === 'sendTestCode') {
						const inboxId = this.getNodeParameter('inboxId', itemIndex) as string;
						const result = (await mailsocketApiRequest.call(
							this,
							'POST',
							`/inboxes/${inboxId}/test-code`,
						)) as { data: IDataObject };
						responseData = result.data;
					} else if (operation === 'list') {
						const returnAll = this.getNodeParameter('returnAll', itemIndex, false) as boolean;
						const limit = this.getNodeParameter('limit', itemIndex, 50) as number;
						responseData = await mailsocketApiRequestAllItems.call(
							this,
							'/inboxes',
							{},
							{ returnAll, total: limit },
						);
					} else {
						throw new NodeOperationError(this.getNode(), `Unknown inbox operation: ${operation}`, {
							itemIndex,
						});
					}
				} else if (resource === 'message') {
					if (operation === 'waitForOtp' || operation === 'waitForLink') {
						const inboxId = this.getNodeParameter('inboxId', itemIndex) as string;
						const since = this.getNodeParameter('since', itemIndex, '0') as string;
						const timeoutSeconds = this.getNodeParameter(
							'timeoutSeconds',
							itemIndex,
							DEFAULT_WAIT_TIMEOUT_SECONDS,
						) as number;
						const onTimeout = this.getNodeParameter('onTimeout', itemIndex, 'error') as string;
						const minConfidence =
							operation === 'waitForOtp'
								? (this.getNodeParameter('minConfidence', itemIndex, 0) as number)
								: undefined;

						const outcome = await waitForMessage.call(this, {
							inboxId,
							require: operation === 'waitForOtp' ? 'otp' : 'link',
							timeoutSeconds,
							since,
							minConfidence,
						});

						if (outcome.status === 'timeout') {
							if (onTimeout === 'returnEmpty') {
								responseData = { timed_out: true };
							} else {
								throw new NodeOperationError(
									this.getNode(),
									`No matching message arrived within ${timeoutSeconds}s.`,
									{ itemIndex },
								);
							}
						} else {
							const message = outcome.data;
							responseData =
								operation === 'waitForOtp'
									? {
											otp: message.otp,
											confidence: message.otp_confidence,
											subject: message.subject,
											from: message.from,
											message_id: message.id,
											received_at: message.received_at,
										}
									: {
											magic_link: message.magic_link,
											subject: message.subject,
											from: message.from,
											message_id: message.id,
										};
						}
					} else if (operation === 'getLatest') {
						const inboxId = this.getNodeParameter('inboxId', itemIndex) as string;
						const onEmpty = this.getNodeParameter('onEmpty', itemIndex, 'returnEmpty') as string;
						try {
							const result = (await mailsocketApiRequest.call(
								this,
								'GET',
								`/inboxes/${inboxId}/messages/latest`,
							)) as { data: IDataObject };
							responseData = result.data;
						} catch (error) {
							const httpCode = (error as { httpCode?: string }).httpCode;
							if (httpCode === '404' && onEmpty === 'returnEmpty') {
								responseData = { found: false };
							} else if (error instanceof NodeApiError) {
								throw new NodeApiError(this.getNode(), error as unknown as JsonObject);
							} else if (error instanceof NodeOperationError) {
								throw new NodeOperationError(this.getNode(), error, { itemIndex });
							} else {
								throw new NodeApiError(this.getNode(), error as JsonObject);
							}
						}
					} else if (operation === 'list') {
						const inboxId = this.getNodeParameter('inboxId', itemIndex) as string;
						const returnAll = this.getNodeParameter('returnAll', itemIndex, false) as boolean;
						const limit = this.getNodeParameter('limit', itemIndex, 50) as number;
						const hasOtp = this.getNodeParameter('hasOtp', itemIndex, false) as boolean;
						const subjectContains = this.getNodeParameter(
							'subjectContains',
							itemIndex,
							'',
						) as string;
						const from = this.getNodeParameter('from', itemIndex, '') as string;

						const qs: IDataObject = {};
						if (hasOtp) qs.has_otp = 'true';
						if (subjectContains) qs.subject_contains = subjectContains;
						if (from) qs.from = from;

						responseData = await mailsocketApiRequestAllItems.call(
							this,
							`/inboxes/${inboxId}/messages`,
							qs,
							{ returnAll, total: limit },
						);
					} else {
						throw new NodeOperationError(this.getNode(), `Unknown message operation: ${operation}`, {
							itemIndex,
						});
					}
				} else {
					throw new NodeOperationError(this.getNode(), `Unknown resource: ${resource}`, {
						itemIndex,
					});
				}

				if (Array.isArray(responseData)) {
					returnData.push(
						...responseData.map((json) => ({ json, pairedItem: { item: itemIndex } })),
					);
				} else {
					returnData.push({ json: responseData, pairedItem: { item: itemIndex } });
				}
			} catch (error) {
				if (this.continueOnFail()) {
					returnData.push({
						json: { error: (error as Error).message },
						pairedItem: { item: itemIndex },
					});
					continue;
				}
				if (error instanceof NodeApiError) {
					throw new NodeApiError(this.getNode(), error as unknown as JsonObject);
				}
				if (error instanceof NodeOperationError) {
					throw new NodeOperationError(this.getNode(), error, { itemIndex });
				}
				throw new NodeOperationError(this.getNode(), error as Error, { itemIndex });
			}
		}

		return [returnData];
	}
}
