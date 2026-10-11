# Installation and activation

Pip install megasmoves installs the Python package, its dependencies, both terminal launchers, the action engine, browser control capability and the MegasMCP stdio service.

The Python packaging ecosystem intentionally provides no portable automatic post-install hook for configuring external apps or starting background services. Therefore:

1. MCP is **ready immediately after package installation** without any Megas enable switch.
2. The local MCP process is **activated automatically by an MCP client** when that client launches megas connect as its configured stdio command.
3. Running megas connect manually also serves MCP on standard input/output until disconnection.

Typical client registration:

~~~json
{"mcpServers":{"megasmoves":{"command":"megas","args":["connect"]}}}
~~~

The default package installs Playwright, and browser tools appear in the MCP tool list. Browser binaries are external assets. If Chromium is missing, run:

~~~sh
python -m playwright install chromium
~~~

The native Windows GUI is independently packaged and must already be installed before megas gui can launch MegasMoves.exe. The pip wheel is not a Windows application installer.

Browser actions are not automatically performed on installation. Tools execute only when the authorized client calls them. The current Community browser tools are a narrower preview than the complete private development browser runtime.
