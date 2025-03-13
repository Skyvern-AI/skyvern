import { INodeType, INodeTypeDescription } from 'n8n-workflow';

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
                default: 'create',
                options: [
                    {
                        name: 'Create a Task',
                        value: 'create',
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
            },
            {
                displayName: 'User Prompt',
                name: 'userPrompt',
                type: 'string',
                required: true,
                default: '',
                placeholder: 'eg: Navigate to the Hacker News homepage and get the top 3 posts.',
                displayOptions: {
                    show: {
                        resource: ['task'],
                        taskOperation: ['create'],
                    },
                },
            },
            {
                displayName: 'URL',
                name: 'url',
                type: 'string',
                default: '',
                placeholder: 'eg: https://news.ycombinator.com/',
                displayOptions: {
                    show: {
                        resource: ['task'],
                        taskOperation: ['create'],
                    },
                },
            },
            {
                displayName: 'Task ID',
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
            },
            {
                displayName: 'Task Version',
                name: 'taskVersion',
                type: 'collection',
                placeholder: 'Choose Version',
                default: {},
                options: [
                    {
                        displayName: 'Task Version',
                        name: 'taskVersion',
                        type: 'options',
                        default: 'v2',
                        options: [
                            {
                                name: 'V1',
                                value: 'v1',
                            },
                            {
                                name: 'V2',
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