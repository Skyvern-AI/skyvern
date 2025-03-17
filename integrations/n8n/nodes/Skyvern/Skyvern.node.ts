import { IDataObject, IExecuteSingleFunctions, IHttpRequestMethods, IHttpRequestOptions, INodeType, INodeTypeDescription } from 'n8n-workflow';
const fetch = require('node-fetch');

export class Skyvern implements INodeType {
    description: INodeTypeDescription = {
        displayName: 'Skyvern',
        name: 'skyvern',
        group: ['transform'],
        description: 'Node to interact with Skyvern',
        defaults: {
            name: 'Skyvern',
        },
        inputs: ['main'],
        outputs: ['main'],
        credentials: [
            {
                name: 'skyvernApi',
                required: true,
            },
        ],
        properties: [
            {
                displayName: 'Resource',
                name: 'resource',
                type: 'options',
				noDataExpression: true,
                options: [
                    {
                        name: 'Task',
                        value: 'task',
                    },
                    {
                        name: 'Workflow',
                        value: 'workflow',
                    },
                    
                ],
                default: 'task',
            },
            {
                displayName: 'Operation',
                name: 'taskOperation',
                type: 'options',
                required: true,
                default: 'dispatch',
                options: [
                    {
                        name: 'Dispatch a Task',
                        value: 'dispatch',
                    },
                    {
                        name: 'Get a Task',
                        value: 'get',
                    },
                ],
                displayOptions: {
                    show: {
                        resource: ['task'],
                    },
                },
                routing: {
                    request: {
                        baseURL: '={{$credentials.baseUrl}}',
                        method: '={{ $value === "dispatch" ? "POST" : "GET" }}' as IHttpRequestMethods,
                        url: '={{"/api/" + ($parameter["taskOptions"]["engine"] ? $parameter["taskOptions"]["engine"] : "v2") + "/tasks"}}',
                    },
                    send: {
                        preSend: [
                            async function (this: IExecuteSingleFunctions, requestOptions: IHttpRequestOptions): Promise<IHttpRequestOptions>  {
                                const taskOperation = this.getNodeParameter('taskOperation');
                                if (taskOperation === "get") return requestOptions;

                                const taskOptions: IDataObject = this.getNodeParameter('taskOptions') as IDataObject;
                                if (taskOptions["engine"] !== "v1") return requestOptions;

                                // trigger the generate task v1 logic
                                const credentials = await this.getCredentials('skyvernApi');
                                const userPrompt = this.getNodeParameter('userPrompt');
                                const response = await fetch(credentials['baseUrl'] + '/api/v1/generate/task', {
                                    method: 'POST',
                                    headers: {
                                        'Content-Type': 'application/json',
                                        'x-api-key': credentials['apiKey'],
                                    },
                                    body: JSON.stringify({
                                        prompt: userPrompt,
                                    }),
                                });
                                if (!response.ok) {
                                    throw new Error('Request to generate Task V1 failed');
                                }

                                const data = await response.json();
                                requestOptions.body = {
                                    url: data.url,
                                    navigation_goal: data.navigation_goal,
                                    navigation_payload: data.navigation_payload,
                                    data_extraction_goal: data.data_extraction_goal,
                                    extracted_information_schema: data.extracted_information_schema,
                                };
                                return requestOptions;
                            },
                        ],
                    },
                },
            },
            {
                displayName: 'User Prompt',
                description: 'The prompt for Skyvern to execute',
                name: 'userPrompt',
                type: 'string',
                required: true,
                default: '',
                placeholder: 'eg: Navigate to the Hacker News homepage and get the top 3 posts.',
                displayOptions: {
                    show: {
                        resource: ['task'],
                        taskOperation: ['dispatch'],
                    },
                },
                routing: {
                    request: {
                        body: {
                            user_prompt: '={{$value}}',
                        },
                    },
                },
            },
            {
                displayName: 'URL',
                description: 'The URL to navigate to',
                name: 'url',
                type: 'string',
                default: '',
                placeholder: 'eg: https://news.ycombinator.com/',
                displayOptions: {
                    show: {
                        resource: ['task'],
                        taskOperation: ['dispatch'],
                    },
                },
                routing: {
                    request: {
                        body: {
                            url: '={{$value ? $value : null}}',
                        },
                    },
                },
            },
            {
                displayName: 'Task ID',
                description: 'The ID of the task',
                name: 'taskId',
                type: 'string',
                required: true,
                default: '',
                displayOptions: {
                    show: {
                        resource: ['task'],
                        taskOperation: ['get'],
                    },
                },
                routing: {
                    request: {
                        method: 'GET',
                        url: '={{"/api/" + ($parameter["taskOptions"]["engine"] ? $parameter["taskOptions"]["engine"] : "v2") + "/tasks/" + $value}}',
                    },
                },
            },
            {
                displayName: 'Task Options',
                name: 'taskOptions',
                type: 'collection',
                placeholder: 'Add Task Options',
                default: {},
                options: [
                    {
                        displayName: 'Engine',
                        name: 'engine',
                        type: 'options',
                        default: 'v2',
                        options: [
                            {
                                name: 'TaskV1',
                                value: 'v1',
                            },
                            {
                                name: 'TaskV2',
                                value: 'v2',
                            },
                        ],
                    },
                ],
                displayOptions: {
                    show: {
                        resource: ['task'],
                    },
                },
            },

        ],
        version: 1,
    };
}