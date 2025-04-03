<!-- DOCTOC SKIP -->

- [Model Context Protocol (MCP)](#model-context-protocol-mcp)
  - [Integration Options](#integration-options)
  - [Supported Applications](#supported-applications)

<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="integrations/mcp/images/SkyvernMCP.png"/>
    <img height="120" src="integrations/mcp/images/SkyvernMCP.png"/>
  </picture>
 <br />
</h1>

# Model Context Protocol (MCP)

Skyvern provides an MCP server implementation that seamlessly integrates with applications so your application gets access to the browser, fetching any live information from the browser and take actions through Skyvern's browser agent.

## Integration Options

You can connect your MCP-enabled applications to Skyvern in two ways:
1. **Local Skyvern Server**
   - Configure your applications to connect to skyvern server running on the localhost
   - To run Skyvern server locally: `skyvern run server`

2. **Skyvern Cloud**
   - Configure your applications to connect to Skyvern Cloud
   - Create an account at [cloud.skyvern.com](https://cloud.skyvern.com)
   - Get the API key from the settings page which will be used for setup

Follow the [installation instructions](#local) to set up. 

## Supported Applications
- Cursor
- Windsurf
- Claude Desktop

`skyvern init` helps you set up the MCP config files for these supported applications automatically - no need to copy-paste the config files. In case you want to set up Skyvern for any other MCP-enabled application, here's the config:
```
{
  "mcpServers": {
    "Skyvern": {
      "env": {
        "SKYVERN_BASE_URL": "https://api.skyvern.com", # "http://localhost:8000" if running locally
        "SKYVERN_API_KEY": "YOUR_SKYVERN_API_KEY" # find the local SKYVERN_API_KEY in the .env file after running `skyvern init`
      },
      "command": "PATH_TO_PYTHON",
      "args": [
        "-m",
        "skyvern",
        "run",
        "mcp"
      ]
    }
  }
}
```