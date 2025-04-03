<!-- DOCTOC SKIP -->

<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/SkyvernMCP.png"/>
    <img src="images/SkyvernMCP.png" alt="Skyvern MCP Logo" width="75%"/>
  </picture>
</h1>

# Model Context Protocol (MCP)

Skyvern's MCP server implementation helps connect your AI Applications to the browser. This allows your AI applications to do things like: Fill out forms, download files, research information on the web, and more.

You can connect your MCP-enabled applications to Skyvern in two ways:
1. **Local Skyvern Server**
   - Use your favourite LLM to power Skyvern
2. **Skyvern Cloud**
   - Create an account at [app.skyvern.com](https://app.skyvern.com)
   - Get the API key from the settings page which will be used for setup

## Quickstart
> ⚠️ **REQUIREMENT**: Skyvern only runs in Python 3.11 environment today ⚠️

1. **Install Skyvern**
	```bash
	pip install skyvern
	```

2. **Configure Skyvern** Run the setup wizard which will guide you through the configuration process. You can connect to either [Skyvern Cloud](https://app.skyvern.com) or a local version of Skyvern. 
	```bash
	skyvern init
	```

3. **(Optional) Launch the Skyvern Server. Only required in local mode** 
	```bash
	skyvern run server
	```

## Examples
### Skyvern allows Claude to look up the top Hackernews posts today

https://github.com/user-attachments/assets/0c10dd96-c6ff-4b99-ad99-f34a5afd04fe

### Cursor looking up the top programming jobs in your area

https://github.com/user-attachments/assets/084c89c9-6229-4bac-adc9-6ad69b41327d

### Ask Windsurf to do a form 5500 search and download some files 

https://github.com/user-attachments/assets/70cfe310-24dc-431a-adde-e72691f198a7

## Supported Applications
`skyvern init` helps configure the following applications for you:
- Cursor
- Windsurf
- Claude Desktop
- Your custom MCP App?

Use the following config if you want to set up Skyvern for any other MCP-enabled application
```json
{
  "mcpServers": {
    "Skyvern": {
      "env": {
        "SKYVERN_BASE_URL": "https://api.skyvern.com", # "http://localhost:8000" if running locally
        "SKYVERN_API_KEY": "YOUR_SKYVERN_API_KEY" # find the local SKYVERN_API_KEY in the .env file after running `skyvern init` or in your Skyvern Cloud console
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
