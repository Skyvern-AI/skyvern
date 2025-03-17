import { IDataObject, IExecuteSingleFunctions, IHttpRequestMethods, IHttpRequestOptions, ILoadOptionsFunctions, INodePropertyOptions, INodeType, INodeTypeDescription, NodeConnectionType, ResourceMapperFields } from 'n8n-workflow';
const fetch = require('node-fetch');

export class Skyvern implements INodeType {
    description: INodeTypeDescription = {
        displayName: 'Skyvern',
        name: 'skyvern',
        icon: 'file:skyvern.png',
        group: ['transform'],
        description: 'Node to interact with Skyvern',
        defaults: {
            name: 'Skyvern',
        },
        inputs: [NodeConnectionType.Main],
        outputs: [NodeConnectionType.Main],
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
                        description: 'Dispatch a task to execute asynchronously',
                    },
                    {
                        name: 'Get a Task',
                        value: 'get',
                        description: 'Get a task by ID',
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
                description: 'Optional Configuration for the task',
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
            {
                displayName: 'Workflow Title',
                description: 'The title of the workflow',
                name: 'workflowId',
                type: 'options',
                typeOptions: {
                    loadOptionsMethod: 'getWorkflows',
                    loadOptionsDependsOn: ['resource'],
                },
                required: true,
                default: '',
                displayOptions: {
                    show: {
                        resource: ['workflow'],
                    },
                },
            },
            {
                displayName: 'Workflow Operation',
                name: 'workflowOperation',
                type: 'options',
                required: true,
                default: 'get',
                options: [
                    {
                        name: 'Get a Workflow Run',
                        value: 'get',
                        description: 'Get a workflow run by ID',
                    },
                    {
                        name: 'Dispatch a Workflow Run',
                        value: 'dispatch',
                        description: 'Dispatch a workflow run to execute asynchronously',
                    },
                ],
                displayOptions: {
                    show: {
                        resource: ['workflow'],
                    },
                },
                routing: {
                    request: {
                        baseURL: '={{$credentials.baseUrl}}',
                        method: '={{ $value === "dispatch" ? "POST" : "GET" }}' as IHttpRequestMethods,
                    },
                },
            },
            {
                displayName: 'Workflow Run ID',
                description: 'The ID of the workflow run',
                name: 'workflowRunId',
                type: 'string',
                required: true,
                default: '',
                displayOptions: {
                    show: {
                        resource: ['workflow'],
                        workflowOperation: ['get'],
                    },
                },
                routing: {
                    request: {
                        url: '={{"/api/v1/workflows/" + $parameter["workflowId"] + "/runs/" + $value}}',
                    },
                },
            },
            // {
            //     displayName: 'Workflow Run Parameters',
            //     name: 'workflowRunParameters',
            //     type: 'json',
            //     description: 'The json-formatted parameters to pass the workflow run to execute',
            //     default: '{}',
            //     displayOptions: {
            //         show: {
            //             resource: ['workflow'],
            //             workflowOperation: ['dispatch'],
            //         },
            //     },
            //     routing: {
            //         request: {
            //             url: '={{"/api/v1/workflows/" + $parameter["workflowId"] + "/run"}}',
            //             body: {
            //                 data: '={{ JSON.parse($value)}}',
            //             },
            //         },
            //     },
            // },
            {
                displayName: 'Workflow Run Parameters',
                name: 'workflowRunParameters',
                type: 'resourceMapper',
                noDataExpression: true,
                description: 'The json-formatted parameters to pass the workflow run to execute',
                required: true,
                default: {
                    mappingMode: 'defineBelow',
                    value: null,
                },
                displayOptions: {
                    show: {
                        resource: ['workflow'],
                        workflowOperation: ['dispatch'],
                    },
                },
                typeOptions: {
                    loadOptionsDependsOn: ['workflowId'],
                    resourceMapper: {
                        resourceMapperMethod: 'getWorkflowRunParameters',
                        mode: 'update',
                        fieldWords: {
                            singular: 'workflowRunParameter',
                            plural: 'workflowRunParameters',
                        },
                        addAllFields: true,
                        multiKeyMatch: true,
                    },
                },
                routing: {
                    request: {
                        url: '={{"/api/v1/workflows/" + $parameter["workflowId"] + "/run"}}',
                        body: {
                            data: '={{ JSON.parse($value)}}',
                        },
                    },
                },
            },
        ],
        version: 1,
    };
    
    methods = {
        loadOptions: {
            async getWorkflows(this: ILoadOptionsFunctions): Promise<INodePropertyOptions[]> {
                const resource = this.getCurrentNodeParameter('resource') as string;
                if (resource !== 'workflow') return [];

                const credentials = await this.getCredentials('skyvernApi');
                const response = await fetch(credentials['baseUrl'] + '/api/v1/workflows?page_size=100', {
                    headers: {
                        'x-api-key': credentials['apiKey'],
                    },
                });
                if (!response.ok) {
                    throw new Error('Request to get workflows failed');
                }
                const data = await response.json();
                return data.map((workflow: any) => ({
                    name: workflow.title,
                    value: workflow.workflow_id,
                }));
            },
        },
        resourceMapping: {
            async getWorkflowRunParameters(this: ILoadOptionsFunctions): Promise<ResourceMapperFields> {
                return {
                    fields: [
                        {
                            id: 'test',
                            displayName: 'test',
                            defaultMatch: true,
                            canBeUsedToMatch: true,
                            required: true,
                            display: true,
                            type: 'string',
                        },
                        {
                            id: 'test2',
                            displayName: 'test2',
                            defaultMatch: true,
                            canBeUsedToMatch: true,
                            required: true,
                            display: true,
                            type: 'string',
                        },
                    ],
                }
            },
        },
    }
}