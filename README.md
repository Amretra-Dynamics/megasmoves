# MegasMoves

**Your always-on SI agent for all your devices.**

Developed by **Amretra Dynamics**. This is a curated, MIT-licensed Community source release candidate, not the full production Windows/Android/cloud product.

> **Not yet published:** Keep the repository private until the owner signs off on the complete release checks. The Python distribution is not yet published on PyPI.

## Install

Once published to PyPI, the intended installation command is:

~~~sh
python -m pip install megasmoves
~~~

For now, from a local checkout:

~~~sh
python -m pip install -e .
~~~

Both terminal commands use the same MegasMoves entrypoint:

~~~sh
megas --version
megasmoves --version
~~~

On Windows, either command without arguments attempts to launch an already-installed native MegasMoves.exe. The Windows GUI is not bundled in the Python wheel; it must be installed from a verified desktop release.

## MCP is enabled by default

No extra feature flag or external product login is required to enable local MegasMCP.

~~~sh
megas connect
megasmoves connect
~~~

Both launch the standard local MCP stdio server; megas serve is an alias.

An MCP client must launch the command as its local server:

~~~json
{"mcpServers":{"megasmoves":{"command":"megas","args":["connect"]}}}
~~~

Pip installation installs the MCP server but does not run post-install commands, silently register it with third-party clients, or create a permanent background service. **MCP becomes ready immediately on install and active when its client starts the command.**

## Megas actions (original local action engine)

These operations use the existing Megas actions and task store rather than creating another project system.

~~~sh
megas actions list
megas actions create "Build website" --path .
megas actions get ACTION_ID
megas actions task-create ACTION_ID "Review release"
megas actions task-move ACTION_ID TASK_ID
~~~

Paths must be existing local files or folders. Creating an action does not start execution.

MCP exposes megas_action_list, megas_action_get, megas_action_create, megas_action_update, megas_action_task_create and megas_action_task_move.

## Browser tools enabled by default

The standard Python package includes Playwright. Browser capability is advertised over MCP without an additional enable flag.

~~~sh
megas browser status
~~~

MCP browser tools: browser_status, browser_start, browser_open, browser_observe, browser_click, browser_fill, browser_close. A local browser session is created only when requested, and is stopped on MCP disconnect. An available Chromium binary is required. To install it on a clean system:

~~~sh
python -m playwright install chromium
~~~

The package does not silently launch websites or modify browser accounts during installation.

## Native GUI and additional commands

~~~sh
megas gui
megas status
megas add .
megas projects
megas check WORKSPACE_ID
megas remove WORKSPACE_ID
~~~

Native GUI startup requires an already installed, verified Windows MegasMoves.exe. The Python wheel alone cannot supply that desktop application.

## Development and release safety

~~~sh
python -m unittest discover -s tests -v
python -m pip wheel --no-deps -w dist .
~~~

Release gates: [Security](SECURITY.md), [source scope](docs/SOURCE_SCOPE.md), [public checklist](docs/RELEASE_CHECKLIST.md), [installation semantics](docs/MCP_INSTALLATION.md), [provenance](docs/PROVENANCE.md).

The planned paid Megas WoW service is separate from this open-source preview.

Community: https://t.me/megasmovesSI
