import {
    FieldType,
    IDataObject,
    IExecuteSingleFunctions,
    IHttpRequestMethods,
    IHttpRequestOptions,
    ILoadOptionsFunctions,
    INodePropertyOptions,
    INodeType,
    INodeTypeDescription,
    NodeConnectionType,
    ResourceMapperField,
    ResourceMapperFields,
} from 'n8n-workflow';

async function skyvernApiRequest(
    this: IExecuteSingleFunctions | ILoadOptionsFunctions,
    method: IHttpRequestMethods,
    endpoint: string,
    body: IDataObject | undefined = undefined,
): Promise<any> {
    const credentials = await this.getCredentials('skyvernApi');
    const options: IHttpRequestOptions = {
        baseURL: credentials.baseUrl as string,
        method,
        url: endpoint,
        body,
        json: true,
    };
    return this.helpers.requestWithAuthentication.call(this, 'skyvernApi', options);
}

export class Skyvern implements INodeType {
    description: INodeTypeDescription = {
        displayName: 'Skyvern',
        name: 'skyvern',
        icon: 'file:skyvern.svg',
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
            // Task Operations
            {
                displayName: 'Operation',
                name: 'taskOperation',
                type: 'options',
                noDataExpression: true,
                required: true,
                default: 'dispatchTask',
                displayOptions: {
                    show: {
                        resource: ['task'],
                    },
                },
                options: [
                    {
                        name: 'Dispatch a Task',
                        value: 'dispatchTask',
                        action: 'Dispatch a task to execute asynchronously',
                        description: 'Dispatch a task to execute asynchronously',
                    },
                    {
                        name: 'Get a Task',
                        value: 'getTask',
                        action: 'Get a task by ID',
                        description: 'Get a task by ID',
                    },
                ],
                routing: {
                    request: {
                        baseURL: '={{$credentials.baseUrl}}',
                        method: '={{ $value === "dispatchTask" ? "POST" : "GET" }}' as IHttpRequestMethods,
                        url: '={{"/v1/run/tasks"}}',
                    },
                    send: {
                        preSend: [
                            async function (
                                this: IExecuteSingleFunctions,
                                requestOptions: IHttpRequestOptions,
                            ): Promise<IHttpRequestOptions> {
                                const taskOperation = this.getNodeParameter('taskOperation');
                                if (taskOperation === "getTask") return requestOptions;

                                const taskOptions = this.getNodeParameter('taskOptions') as IDataObject;
                                const legacy_engine = taskOptions['engine'] as string | null;
                                if (legacy_engine === 'v1') {
                                    (requestOptions.body as IDataObject)['engine'] = 'skyvern-1.0';
                                } else if (legacy_engine === 'v2') {
                                    (requestOptions.body as IDataObject)['engine'] = 'skyvern-2.0';
                                }
                                return requestOptions;
                            },
                        ],
                    },
                },
            },
            // Workflow Operations
            {
                displayName: 'Operation',
                name: 'workflowOperation',
                type: 'options',
                noDataExpression: true,
                required: true,
                default: 'getWorkflow',
                displayOptions: {
                    show: {
                        resource: ['workflow'],
                    },
                },
                options: [
                    {
                        name: 'Get a Workflow Run',
                        value: 'getWorkflow',
                        action: 'Get a workflow run by ID',
                        description: 'Get a workflow run by ID',
                    },
                    {
                        name: 'Dispatch a Workflow Run',
                        value: 'dispatchWorkflow',
                        action: 'Dispatch a workflow run to execute asynchronously',
                        description: 'Dispatch a workflow run to execute asynchronously',
                    },
                ],
                routing: {
                    request: {
                        baseURL: '={{$credentials.baseUrl}}',
                        method: '={{ $value === "dispatchWorkflow" ? "POST" : "GET" }}' as IHttpRequestMethods,
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
                        taskOperation: ['dispatchTask'],
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
                        taskOperation: ['dispatchTask'],
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
                        taskOperation: ['dispatchTask'],
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
                        taskOperation: ['getTask'],
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
                        taskOperation: ['dispatchTask'],
                    },
                },
            },
            {
                displayName: 'Workflow Name or ID',
                description: 'Choose from the list, or specify an ID using an <a href="https://docs.n8n.io/code/expressions/">expression</a>',
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
                displayName: 'Workflow Run ID',
                description: 'The ID of the workflow run',
                name: 'workflowRunId',
                type: 'string',
                required: true,
                default: '',
                displayOptions: {
                    show: {
                        resource: ['workflow'],
                        workflowOperation: ['getWorkflow'],
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
                        workflowOperation: ['dispatchWorkflow'],
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
                        workflowOperation: ['dispatchWorkflow'],
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

                const response = await skyvernApiRequest.call(
                    this,
                    'GET',
                    '/api/v1/workflows?page_size=100',
                );
                const data = response;
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

                const operation = this.getCurrentNodeParameter('workflowOperation') as string;
                if (operation !== 'dispatchWorkflow') return { fields: [] };

                const workflowId = this.getCurrentNodeParameter('workflowId') as string;
                if (!workflowId) return { fields: [] };

                const workflow = await skyvernApiRequest.call(
                    this,
                    'GET',
                    `/api/v1/workflows/${workflowId}`,
                );
                const parameters: any[] = workflow.workflow_definition.parameters;

                const fields: ResourceMapperField[] = await Promise.all(
                    parameters
                        .filter((parameter: any) => parameter.parameter_type === 'workflow' || parameter.parameter_type === 'credential')
                        .map(async (parameter: any) => {
                            let options: INodePropertyOptions[] | undefined = undefined;
                            let parameterType: FieldType | undefined = undefined;
                            if (parameter.parameter_type === 'credential') {
                                const credData = await skyvernApiRequest.call(
                                    this,
                                    'GET',
                                    '/api/v1/credentials',
                                );
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
