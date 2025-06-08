import { FieldType, IDataObject, IExecuteSingleFunctions, IHttpRequestMethods, IHttpRequestOptions, ILoadOptionsFunctions, INodePropertyOptions, INodeType, INodeTypeDescription, NodeConnectionType, ResourceMapperField, ResourceMapperFields } from 'n8n-workflow';


async function makeRequest(
    this: ILoadOptionsFunctions | IExecuteSingleFunctions,
    url: string,
    options: IHttpRequestOptions = {},
): Promise<{ ok: boolean; json: () => Promise<any> }> {
    const requestOptions: IHttpRequestOptions = {
        url,
        method: options.method ?? 'GET',
        headers: options.headers,
        body: options.body,
        qs: options.qs,
        json: true,
        resolveWithFullResponse: true,
        simple: false,
    };

    const response = await this.helpers.httpRequest(requestOptions);

    return {
        ok: response.statusCode >= 200 && response.statusCode < 300,
        json: async () => response.body,
    };
}

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
                name: 'operation',
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
                        url: '={{"/v1/run/tasks"}}',
                    },
                    send: {
                        preSend: [
                            async function (this: IExecuteSingleFunctions, requestOptions: IHttpRequestOptions): Promise<IHttpRequestOptions>  {
                                const operation = this.getNodeParameter('operation');
                                if (operation === "get") return requestOptions;

                                const taskOptions: IDataObject = this.getNodeParameter('taskOptions') as IDataObject;
                                const legacy_engine = taskOptions["engine"] as string | null
                                if (legacy_engine === "v1") {
                                    (requestOptions.body as IDataObject)['engine'] = "skyvern-1.0";
                                }else if (legacy_engine === "v2") {
                                    (requestOptions.body as IDataObject)['engine'] = "skyvern-2.0";
                                }
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
                        operation: ['dispatch'],
                    },
                },
                routing: {
                    request: {
                        body: {
                            prompt: '={{$value}}',
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
                        operation: ['dispatch'],
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
                displayName: 'Webhook Callback URL',
                description: 'Optional URL that Skyvern will call when the task finishes',
                name: 'webhookUrl',
                type: 'string',
                default: '',
                placeholder: 'https://example.com/webhook',
                displayOptions: {
                    show: {
                        resource: ['task'],
                        operation: ['dispatch'],
                    },
                },
                routing: {
                    request: {
                        body: {
                            webhook_url: '={{$value ? $value : null}}',
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
                        operation: ['get'],
                    },
                },
                routing: {
                    request: {
                        method: 'GET',
                        url: '={{"/v1/runs/" + $value}}',
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
                        displayName: 'Engine(Deprecated)',
                        description: 'Deprecated: please migrate to use "Engine" option',
                        name: 'engine',
                        type: 'options',
                        default: '',
                        options: [
                            {
                                name: 'TaskV1',
                                value: 'v1',
                            },
                            {
                                name: 'TaskV2',
                                value: 'v2',
                            },
                            {
                                name: 'THIS FIELD IS DEPRECATED',
                                value: '',
                            },
                        ],
                    },
                    {
                        displayName: 'Engine',
                        name: 'runEngine',
                        type: 'options',
                        default: 'skyvern-2.0',
                        options: [
                            {
                                name: 'Skyvern 1.0',
                                value: 'skyvern-1.0',
                            },
                            {
                                name: 'Skyvern 2.0',
                                value: 'skyvern-2.0',
                            },
                            {
                                name: 'OpenAI CUA',
                                value: 'openai-cua',
                            },
                            {
                                name: 'Anthropic CUA',
                                value: 'anthropic-cua',
                            }
                        ],
                        routing: {
                            request: {
                                body: {
                                    engine: '={{$value}}',
                                },
                            },
                        },
                    }
                ],
                displayOptions: {
                    show: {
                        resource: ['task'],
                        operation: ['dispatch'],
                    },
                },
            },
            {
                displayName: 'Workflow Title or ID',
                description: 'The title of the workflow. Choose from the list, or specify an ID using an <a href="https://docs.n8n.io/code-examples/expressions/">expression</a>.',
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
                name: 'operation',
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
                        operation: ['get'],
                    },
                },
                routing: {
                    request: {
                        url: '={{"/api/v1/workflows/" + $parameter["workflowId"] + "/runs/" + $value}}',
                    },
                },
            },
            {
                displayName: 'Workflow Run Parameters',
                name: 'workflowRunParameters',
                type: 'resourceMapper',
                noDataExpression: true,
                description: 'The JSON-formatted parameters to pass the workflow run to execute',
                required: true,
                default: {
                    mappingMode: 'defineBelow',
                    value: null,
                },
                displayOptions: {
                    show: {
                        resource: ['workflow'],
                        operation: ['dispatch'],
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
                            data: '={{$value["value"]}}',
                        },
                    },
                },
            },
            {
                displayName: 'Webhook Callback URL',
                description: 'Optional URL that Skyvern will call when the workflow run finishes',
                name: 'webhookCallbackUrl',
                type: 'string',
                default: '',
                placeholder: 'https://example.com/webhook',
                displayOptions: {
                    show: {
                        resource: ['workflow'],
                        operation: ['dispatch'],
                    },
                },
                routing: {
                    request: {
                        body: {
                            webhook_callback_url: '={{$value ? $value : null}}',
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
                const response = await makeRequest.call(this, credentials['baseUrl'] + '/api/v1/workflows?page_size=100', {
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
                    value: workflow.workflow_permanent_id,
                }));
            },
        },
        resourceMapping: {
            async getWorkflowRunParameters(this: ILoadOptionsFunctions): Promise<ResourceMapperFields> {
                const resource = this.getCurrentNodeParameter('resource') as string;
                if (resource !== 'workflow') return { fields: [] };

                const operation = this.getCurrentNodeParameter('operation') as string;
                if (operation !== 'dispatch') return { fields: [] };

                const workflowId = this.getCurrentNodeParameter('workflowId') as string;
                if (!workflowId) return { fields: [] };

                const credentials = await this.getCredentials('skyvernApi');
                const response = await makeRequest.call(this, credentials['baseUrl'] + '/api/v1/workflows/' + workflowId, {
                    headers: {
                        'x-api-key': credentials['apiKey'],
                    },
                });
                if (!response.ok) {
                    throw new Error('Request to get workflow failed');
                }
                const workflow = await response.json();
                const parameters: any[] = workflow.workflow_definition.parameters;

                const fields: ResourceMapperField[] = await Promise.all(
                    parameters
                        .filter((parameter: any) => parameter.parameter_type === 'workflow' || parameter.parameter_type === 'credential')
                        .map(async (parameter: any) => {
                            let options: INodePropertyOptions[] | undefined = undefined;
                            let parameterType: FieldType | undefined = undefined;
                            if (parameter.parameter_type === 'credential') {
                                const credResponse = await makeRequest.call(this, credentials['baseUrl'] + '/api/v1/credentials', {
                                    headers: {
                                        'x-api-key': credentials['apiKey'],
                                    },
                                });
                                if (!credResponse.ok) {
                                    throw new Error('Request to get credentials failed');
                                }
                                const credData = await credResponse.json();
                                options = credData.map((credential: any) => ({
                                    name: credential.name,
                                    value: credential.credential_id,
                                }));
                                parameterType = 'options';
                            } else {
                                const parameter_type_map: Record<string, FieldType> = {
                                    string: 'string',
                                    integer: 'number',
                                    float: 'number',
                                    boolean: 'boolean',
                                    json: 'object',
                                    file_url: 'url',
                                }
                                parameterType = parameter_type_map[parameter.workflow_parameter_type];
                            }

                            return {
                                id: parameter.key,
                                displayName: parameter.key,
                                defaultMatch: true,
                                canBeUsedToMatch: false,
                                required: parameter.default_value === undefined || parameter.default_value === null,
                                display: true,
                                type: parameterType,
                                options: options,
                            };
                        })
                );


                // HACK: If there are no parameters, add a empty field to avoid the resource mapper from crashing
                if (fields.length === 0) {
                    fields.push({
                        id: 'NO_PARAMETERS',
                        displayName: 'No Parameters',
                        defaultMatch: false,
                        canBeUsedToMatch: false,
                        required: false,
                        display: true,
                        type: 'string',
                    });
                }

                return {
                    fields: fields,
                }
            },
        },
    }
}
