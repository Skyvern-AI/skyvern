# HTTP Request Block for Skyvern Workflows

## Overview

The HTTP Request block is a new workflow block type that allows you to make HTTP requests within your Skyvern workflows. This block can parse cURL commands and execute them, making it easy to integrate with APIs and web services.

## Features

- **cURL Command Support**: Simply paste a cURL command and the block will parse and execute it
- **HTTP Methods**: Supports GET, POST, PUT, DELETE, and custom HTTP methods
- **Headers**: Full support for custom headers including authentication
- **Request Body**: Send JSON, form data, or raw text in request body
- **Template Variables**: Use Jinja2 template syntax to reference workflow parameters and outputs from previous blocks
- **Response Handling**: Access response status code, headers, and body in subsequent blocks

## Usage

### Basic Example

```yaml
block_type: "http_request"
label: "api_call"
curl_command: 'curl -X GET "https://api.example.com/data"'
```

### With Authentication

```yaml
block_type: "http_request"
label: "authenticated_api_call"
curl_command: 'curl -X GET "https://api.example.com/user" -H "Authorization: Bearer {{ api_token }}"'
parameters:
  - key: "api_token"
```

### POST Request with JSON Body

```yaml
block_type: "http_request"
label: "create_resource"
curl_command: |
  curl -X POST "https://api.example.com/resources" \
    -H "Content-Type: application/json" \
    -d '{"name": "{{ resource_name }}", "type": "{{ resource_type }}"}'
```

### Using Output from Previous Blocks

```yaml
- block_type: "http_request"
  label: "get_user"
  curl_command: 'curl -X GET "https://api.example.com/user/{{ user_id }}"'

- block_type: "http_request"
  label: "get_user_posts"
  curl_command: 'curl -X GET "{{ get_user_output.body.posts_url }}"'
```

## Output Format

The HTTP block outputs a structured response object:

```json
{
  "status_code": 200,
  "headers": {
    "Content-Type": "application/json",
    "X-Request-Id": "abc123"
  },
  "url": "https://api.example.com/data",
  "method": "GET",
  "body": {
    "data": "response data here"
  }
}
```

## Advanced Options

When using the UI, you can expand "Advanced Options" to override specific parts of the cURL command:

- **Method**: Override the HTTP method
- **URL**: Override the URL
- **Body**: Override the request body
- **Timeout**: Set a custom timeout (default: 30 seconds)

## Error Handling

- The block will fail if the HTTP status code is 400 or higher
- Use `continue_on_failure: true` to continue the workflow even if the request fails
- Timeout errors are handled gracefully with appropriate error messages

## Implementation Details

The HTTP block is implemented as:
- Backend: `HTTPBlock` class in `/workspace/skyvern/forge/sdk/workflow/models/block.py`
- Frontend: `HTTPNode` component in `/workspace/skyvern-frontend/src/routes/workflows/editor/nodes/HTTPNode/`
- Block type: `http_request` in the workflow YAML
- Node type: `http` in the workflow editor

## Example Workflow

See `example_http_workflow.yaml` for a complete example demonstrating:
1. Making authenticated API calls
2. Using response data in subsequent requests
3. Processing API responses with a text prompt block