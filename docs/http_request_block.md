# HTTP Request Block

The HTTP Request block allows you to make HTTP/REST API calls within your Skyvern workflows, similar to the HTTP Request node in n8n. This block can parse curl commands or accept individual HTTP parameters to execute API requests and pass the response data to subsequent workflow steps.

## Features

- **Curl Command Support**: Paste curl commands directly from your browser's developer tools
- **Standard HTTP Methods**: Support for GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS
- **Request Customization**: Set headers, body, timeout, and redirect behavior
- **Template Support**: Use Jinja2 templates to reference values from previous blocks
- **Response Handling**: Automatically parse JSON responses and make them available to subsequent blocks

## Block Configuration

### Using Curl Command

```yaml
- block_type: http_request
  label: my_api_call
  curl_command: 'curl -X GET "https://api.example.com/data" -H "Authorization: Bearer token123"'
```

### Using Individual Parameters

```yaml
- block_type: http_request
  label: my_api_call
  method: POST
  url: https://api.example.com/data
  headers:
    Authorization: Bearer token123
    Content-Type: application/json
  body:
    key: value
    nested:
      data: example
  timeout: 30
  follow_redirects: true
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `curl_command` | string | None | A complete curl command to execute |
| `method` | string | GET | HTTP method (GET, POST, PUT, DELETE, etc.) |
| `url` | string | None | The URL to send the request to |
| `headers` | dict | None | HTTP headers as key-value pairs |
| `body` | dict/string | None | Request body (automatically JSON-encoded for dicts) |
| `timeout` | int | 30 | Request timeout in seconds |
| `follow_redirects` | bool | true | Whether to follow HTTP redirects |
| `parameter_keys` | list | None | List of workflow parameter keys to use |
| `continue_on_failure` | bool | false | Continue workflow even if request fails |

## Output Format

The block outputs a dictionary with the following structure:

```json
{
  "status_code": 200,
  "headers": {
    "Content-Type": "application/json",
    "Server": "nginx"
  },
  "url": "https://api.example.com/data",
  "body": {
    // Response body (parsed JSON or raw text)
  }
}
```

## Usage Examples

### 1. Simple GET Request

```yaml
- block_type: http_request
  label: get_user_data
  method: GET
  url: https://api.github.com/users/octocat
```

### 2. POST Request with JSON Body

```yaml
- block_type: http_request
  label: create_item
  method: POST
  url: https://api.example.com/items
  headers:
    Content-Type: application/json
    API-Key: "{{ api_key }}"
  body:
    name: "New Item"
    description: "Created by Skyvern"
    metadata:
      source: "workflow"
```

### 3. Using Previous Block Output

```yaml
- block_type: http_request
  label: get_user
  method: GET
  url: https://api.example.com/users/123

- block_type: http_request
  label: get_user_posts
  method: GET
  url: "https://api.example.com/users/{{ get_user_output.body.id }}/posts"
  headers:
    Authorization: "Bearer {{ get_user_output.body.access_token }}"
```

### 4. Curl Command from Browser

```yaml
- block_type: http_request
  label: browser_request
  curl_command: |
    curl 'https://api.example.com/v1/data' \
      -H 'accept: application/json' \
      -H 'authorization: Bearer eyJhbGc...' \
      -H 'content-type: application/json' \
      --data-raw '{"filter":"active","limit":10}'
```

### 5. Error Handling with Continue on Failure

```yaml
- block_type: http_request
  label: optional_webhook
  method: POST
  url: https://webhook.site/optional-endpoint
  body:
    event: "workflow_step_completed"
  continue_on_failure: true  # Workflow continues even if this fails
  timeout: 5  # Short timeout for non-critical requests
```

## Template Variables

You can use Jinja2 templates in the following fields:
- `curl_command`
- `url`
- `headers` (values)
- `body` (string values)

Reference previous block outputs using: `{{ block_label_output }}`
Reference workflow parameters using: `{{ parameter_key }}`

## Error Handling

The block will fail if:
- No URL is provided (either via curl_command or url parameter)
- The request times out
- Network errors occur
- Invalid curl command syntax (if using curl_command)

Set `continue_on_failure: true` to allow the workflow to continue even if the HTTP request fails.

## Integration with Other Blocks

The HTTP Request block integrates seamlessly with other Skyvern blocks:

1. **Text Prompt Block**: Analyze API responses using LLM
2. **Code Block**: Process response data with custom Python code
3. **For Loop Block**: Iterate over API response arrays
4. **Send Email Block**: Send notifications based on API responses
5. **Task Blocks**: Use API data to fill forms or navigate websites

## Best Practices

1. **Use Templates**: Leverage Jinja2 templates for dynamic values instead of hardcoding
2. **Set Appropriate Timeouts**: Adjust timeout based on expected API response times
3. **Handle Errors**: Use `continue_on_failure` for non-critical requests
4. **Secure Credentials**: Use workflow parameters for sensitive data like API keys
5. **Parse Responses**: The block automatically parses JSON responses, making data extraction easier