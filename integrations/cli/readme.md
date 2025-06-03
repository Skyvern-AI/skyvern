<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->

- [Skyvern CLI](#skyvern-cli)
  - [Quickstart](#quickstart)
  - [Initialization](#initialization)
    - [Init Browser](#init-browser)
  - [Run](#run)
    - [server](#server)
    - [ui](#ui)
    - [all](#all)
    - [mcp](#mcp)
  - [Workflow](#workflow)
    - [start](#start)
    - [stop](#stop)
    - [status](#status)
    - [list](#list)
  - [Tasks](#tasks)
    - [list](#list-1)
  - [Status](#status-1)
  - [Docs](#docs)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Skyvern CLI

Skyvern ships with a comprehensive command line interface built using `typer`. The
main entry point is the `skyvern` command which exposes subcommands for common
operations. Below is a summary of all available commands.

## Quickstart

```
skyvern quickstart [--no-postgres] [--skip-browser-install] [--server-only]
```

Runs an interactive setup followed by starting the API and UI servers. It checks
that Docker is available, configures the environment and launches the required
services. Use `--server-only` to skip the UI.

## Initialization

```
skyvern init [--no-postgres]
```

Interactive configuration wizard. It sets up the database, generates an API key,
installs the browser and writes necessary values to `.env`.

### Init Browser

```
skyvern init browser
```

Only configure the browser and install Chromium without running the full wizard.

## Run

Group of commands to launch individual services.

### server

```
skyvern run server
```

Start the Skyvern API server.

### ui

```
skyvern run ui
```

Start the UI server. The command ensures no other process is using port `8080`
and installs frontend dependencies if needed.

### all

```
skyvern run all
```

Start both the API server and UI server concurrently.

### mcp

```
skyvern run mcp
```

Run the Model Context Protocol (MCP) server.

## Workflow

Commands for managing workflows through the Skyvern API.

### start

```
skyvern workflow start WORKFLOW_ID [--parameters JSON] [--title TEXT] [--max-steps INTEGER]
```

Dispatch a workflow run using the provided permanent identifier. Parameters are
supplied as a JSON string.

### stop

```
skyvern workflow stop RUN_ID
```

Cancel a running workflow.

### status

```
skyvern workflow status RUN_ID [--tasks]
```

Show the status of a workflow run. Pass `--tasks` to include executed tasks.

### list

```
skyvern workflow list [--page INTEGER] [--page-size INTEGER] [--template]
```

List workflows for the organization.

## Tasks

### list

```
skyvern tasks list --workflow-run-id RUN_ID
```

Return the executed tasks for a workflow run.

## Status

```
skyvern status
```

Display whether the API server, UI server and PostgreSQL database are running.

## Docs

```
skyvern docs
```

Open the Skyvern documentation website in your default browser.
