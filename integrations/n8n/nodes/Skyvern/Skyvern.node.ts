import { FieldType, IDataObject, IExecuteSingleFunctions, IHttpRequestMethods, IHttpRequestOptions, ILoadOptionsFunctions, INodePropertyOptions, INodeType, INodeTypeDescription, NodeConnectionType, ResourceMapperField, ResourceMapperFields } from 'n8n-workflow';
import https from 'https';
import { URL } from 'url';

async function makeRequest(url: string, options: any = {}): Promise<any> {
    return new Promise((resolve, reject) => {
        const parsedUrl = new URL(url);
        const requestOptions = {
            hostname: parsedUrl.hostname,
            path: parsedUrl.pathname + parsedUrl.search,
            method: options.method || 'GET',
            headers: options.headers || {},
        };

        const req = https.request(requestOptions, (res) => {
            let data = '';
            
            res.on('data', (chunk) => {
                data += chunk;
            });
            
            res.on('end', () => {
                if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
                    const response = {
                        ok: true,
                        status: res.statusCode,
                        statusText: res.statusMessage || '',
                        headers: res.headers,
                        json: () => {
                            try {
                                return Promise.resolve(JSON.parse(data));
                            } catch (e) {
                                return Promise.reject(new Error('Invalid JSON response'));
                            }
                        },
                        text: () => Promise.resolve(data),
                        blob: () => Promise.resolve(new Blob([data])),
                        arrayBuffer: () => Promise.resolve(Buffer.from(data)),
                        clone: () => response,
                    };
                    resolve(response);
                } else {
                    reject(new Error(`Request failed with status code ${res.statusCode}`));
                }
            });
        });
        
        req.on('error', (error) => {
            reject(error);
        });
        
        if (options.body) {
            req.write(options.body);
        }
        
        req.end();
    });
}

export class Skyvern implements INodeType {
    description: INodeTypeDescription = {
        displayName: 'Skyvern',
        name: 'skyvern',
        icon: 'file:skyvern.png', // eslint-disable-line
        group: ['transform'],
        description: 'Node to interact with Skyvern',
        defaults: {
            name: 'Skyvern',
        },
        inputs: [NodeConnectionType.Main], // eslint-disable-line
        outputs: [NodeConnectionType.Main], // eslint-disable-line
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
                                const response = await makeRequest(credentials['baseUrl'] + '/api/v1/generate/task', {
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
                                    throw new Error('Request to generate Task V1 failed'); // eslint-disable-line
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
                displayName: 'Workflow Title or ID', // eslint-disable-line
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
                            data: '={{$value["value"]}}',
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
                const response = await makeRequest(credentials['baseUrl'] + '/api/v1/workflows?page_size=100', {
                    headers: {
                        'x-api-key': credentials['apiKey'],
                    },
                });
                if (!response.ok) {
                    throw new Error('Request to get workflows failed'); // eslint-disable-line
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

                const workflowOperation = this.getCurrentNodeParameter('workflowOperation') as string;
                if (workflowOperation !== 'dispatch') return { fields: [] };
               
                const workflowId = this.getCurrentNodeParameter('workflowId') as string;
                if (!workflowId) return { fields: [] };

                const credentials = await this.getCredentials('skyvernApi');
                const response = await makeRequest(credentials['baseUrl'] + '/api/v1/workflows/' + workflowId, {
                    headers: {
                        'x-api-key': credentials['apiKey'],
                    },
                });
                if (!response.ok) {
                    throw new Error('Request to get workflow failed'); // eslint-disable-line
                }
                const workflow = await response.json();
                const parameters: any[] = workflow.workflow_definition.parameters;

                const fields: ResourceMapperField[] = await Promise.all(
                    parameters.filter((parameter: any) => parameter.parameter_type === 'workflow' || parameter.parameter_type === 'credential')
                    .map(async (parameter: any) => {
                        let options: INodePropertyOptions[] | undefined = undefined;
                        let parameterType: FieldType | undefined = undefined;
                        if (parameter.parameter_type === 'credential') {
                            const response = await makeRequest(credentials['baseUrl'] + '/api/v1/credentials', {
                                headers: {
                                    'x-api-key': credentials['apiKey'],
                                },
                            });
                            if (!response.ok) {
                                throw new Error('Request to get credentials failed'); // eslint-disable-line
                            }
                            const data = await response.json();
                            options = data.map((credential: any) => ({
                                name: credential.name,
                                value: credential.credential_id,
                            }));
                            parameterType = 'options';
                        }else{
                            const parameter_type_map: Record<string, string> = {
                                'string': 'string',
                                'integer': 'number',
                                'float': 'number',
                                'boolean': 'boolean',
                                'json': 'json',
                                'file_url': 'url',
                            }
                            parameterType = parameter_type_map[parameter.workflow_parameter_type] as FieldType;
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