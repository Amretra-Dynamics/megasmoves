# MegasMoves

**Your always-on SI agent for all your devices.**

MegasMoves by **Amretra Dynamics** brings local actions, browser automation and Model Context Protocol (MCP) tools together through one command: `megas`.

## Install

Requires Python 3.11 or newer.

```bash
python -m pip install megasmoves
```

For development from a source checkout:

```bash
python -m pip install -e .
```

The Python package is named **megasmoves**. The terminal command is **megas**.

## Connect to an MCP client

```bash
megas connect
```

Configure your MCP-compatible client to launch MegasMoves:

```json
{
  "mcpServers": {
    "megasmoves": {
      "command": "megas",
      "args": ["connect"]
    }
  }
}
```

The client starts the local MCP server over standard input/output. Actions and browser tools are exposed without an additional enable flag.

## Megas actions

Create, organize and inspect local actions and their tasks:

```bash
megas actions list
megas actions create "Build website" --path /absolute/path/to/workspace
megas actions get ACTION_ID
megas actions task-create ACTION_ID "Review changes"
megas actions task-move ACTION_ID TASK_ID
```

Action references point to existing local files or folders. Creating an action does not execute it. Available MCP tools include `megas_action_list`, `megas_action_get`, `megas_action_create`, `megas_action_update`, `megas_action_task_create` and `megas_action_task_move`.

## Browser

```bash
megas browser status
```

Playwright is included as a dependency. Install Chromium if it is not already present:

```bash
python -m playwright install chromium
```

Browser MCP tools include `browser_start`, `browser_open`, `browser_observe`, `browser_click`, `browser_fill` and `browser_close`. Sessions start when requested by the connected client.

## Desktop app

On Windows, launch the installed native MegasMoves application with:

```bash
megas gui
```

Running `megas` without arguments also attempts to open the native desktop application. The native Windows app is distributed separately from the Python package.

## More

```bash
megas --help
megas --version
megas status
```

[Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [MCP setup](docs/MCP_INSTALLATION.md) · [License](LICENSE)

**MIT License** · © Amretra Dynamics
