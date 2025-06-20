# HTTP Request Node

The HTTP Request node allows you to make HTTP/API calls within your Skyvern workflows. It provides a user-friendly interface for configuring API requests with support for various HTTP methods, headers, authentication, and request bodies.

## Features

### 1. Dual Input Modes

The node offers two ways to configure HTTP requests:

#### Manual Mode
- **Method Selection**: Choose from GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS
- **URL Input**: Enter the target URL with template variable support
- **Headers Management**: Add, edit, and remove custom headers
- **Body Configuration**: Send JSON, form data, or plain text
- **Advanced Settings**: Configure timeout and redirect behavior

#### cURL Import Mode
- **Paste & Parse**: Import cURL commands from browser developer tools
- **Automatic Conversion**: Converts cURL syntax to manual configuration
- **Quick Setup**: Ideal for replicating browser requests

### 2. Visual Enhancements

- **Method Color Coding**: Each HTTP method has a distinct color for quick identification
- **Header Count Badge**: Shows the number of configured headers
- **Common Header Shortcuts**: Quick-add buttons for frequently used headers
- **Output Preview**: Displays the expected response structure

### 3. Smart Features

- **URL Validation**: Real-time validation with template variable support
- **JSON Validation**: Validates JSON body syntax (allows template variables)
- **Header Autocomplete**: Suggests common header names
- **Content-Type Helper**: Automatically suggests content types for body

### 4. Template Variable Support

Use `{{ variable_name }}` syntax throughout:
- URL: `https://api.example.com/users/{{ user_id }}`
- Headers: `Authorization: Bearer {{ api_token }}`
- Body: `{ "message": "Hello {{ user_name }}" }`

### 5. Advanced Settings

- **Timeout**: Configure request timeout (1-300 seconds)
- **Follow Redirects**: Toggle automatic redirect following
- **Export to cURL**: Generate cURL command from manual configuration

## Usage Examples

### Basic GET Request
1. Select "GET" method
2. Enter URL: `https://api.github.com/users/octocat`
3. Add header: `Accept: application/json`

### POST with JSON Body
1. Select "POST" method
2. Enter URL: `https://api.example.com/data`
3. Set Content-Type: `application/json`
4. Add JSON body:
   ```json
   {
     "name": "{{ user_name }}",
     "email": "{{ user_email }}"
   }
   ```

### Import from cURL
1. Switch to "Import cURL" tab
2. Paste cURL command from browser
3. Click "Parse & Convert"
4. Review and adjust converted settings

## Output Structure

The HTTP Request node outputs:
```json
{
  "status_code": 200,
  "headers": {
    "content-type": "application/json",
    "x-ratelimit-remaining": "59"
  },
  "url": "https://api.example.com/data",
  "body": {
    "success": true,
    "data": { ... }
  }
}
```

Access output in subsequent blocks:
- Status: `{{ block_label_output.status_code }}`
- Headers: `{{ block_label_output.headers.content-type }}`
- Body: `{{ block_label_output.body.data }}`

## Best Practices

1. **Use Parameters**: Store sensitive data (API keys, tokens) in workflow parameters
2. **Error Handling**: Enable "Continue on Failure" for non-critical requests
3. **Timeouts**: Set appropriate timeouts based on API response times
4. **Headers**: Always set proper Content-Type for request bodies
5. **Testing**: Use the output preview to understand response structure

## Common Use Cases

- **API Integration**: Connect to external services and APIs
- **Webhook Notifications**: Send status updates to webhook endpoints
- **Data Fetching**: Retrieve data from REST APIs
- **Authentication**: Handle OAuth flows and API authentication
- **Data Submission**: Post form data or JSON to endpoints