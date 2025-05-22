import {
	IAuthenticateGeneric,
	ICredentialTestRequest,
	ICredentialType,
	INodeProperties,
} from 'n8n-workflow';

export class SkyvernApi implements ICredentialType {
	name = 'skyvernApi';
	displayName = 'Skyvern API';
	// Uses the link to this tutorial as an example
	// Replace with your own docs links when building your own nodes
	documentationUrl = 'https://docs.skyvern.ai/';
	properties: INodeProperties[] = [
		{
			displayName: 'API Key',
			name: 'apiKey',
			type: 'string',
			typeOptions: { password: true },
			default: '',
		},
        {
            displayName: 'Base URL',
            name: 'baseUrl',
            type: 'string',
            default: 'https://api.skyvern.com',
            placeholder: 'https://api.skyvern.com',
        },
	];
	authenticate: IAuthenticateGeneric = {
		type: 'generic',
		properties: {
            headers: {
                'x-api-key': '={{$credentials.apiKey}}',
                'Content-Type': 'application/json',
            }
		},
	};
	test: ICredentialTestRequest = {
		request: {
			baseURL: '={{$credentials?.baseUrl}}',
			url: '/api/v1/organizations',
		},
	};
}