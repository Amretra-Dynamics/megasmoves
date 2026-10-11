# MegasMoves

**Your always-on SI agent for all your devices.**

**MegasMoves** by **Amretra Dynamics** is a multi-device software automation platform built around native applications, a local execution runtime, the Model Context Protocol (MCP), and explicit device and workspace permissions.

## Architecture

The MegasMoves system spans several layers:

| Layer | Architecture |
| --- | --- |
| **Megas runtime** | Python application and capability orchestration |
| **MegasMCP** | Typed MCP transport and operator interfaces |
| **Megas Browser** | Dedicated Chromium/Playwright operation, session state and approval boundaries |
| **Native desktop** | WinUI 3 on Windows, Swift on macOS, GTK/C++ on Linux |
| **Megas Actions** | Action grouping, local task records and scoped file/folder references |
| **Native execution** | C++ runtime boundary and platform bridges |
| **Android** | Kotlin application and mobile-device surface |

## Developer interface

The CLI entrypoint is **`megas`**. The Python distribution uses the name **`megasmoves`**.

```sh
pip install megasmoves
megas connect
```

`megas connect` starts the local MCP interface for clients that support standard input/output servers.

## Engineering principles

MegasMoves uses typed capability contracts, local permissions and bounded operations to give agents access to software tools and devices. The system includes source-level separation between operation requests, transport, runtime state, native device bridges, and the services that execute them.

The codebase is organized under `worker/` for Megas runtime and native desktop components, with additional product surfaces maintained as part of the overall system architecture.

## Project

- **Company:** Amretra Dynamics
- **Product:** MegasMoves
- **Interface:** `megas`
- **License:** [MIT](LICENSE)
- **Security:** [Security policy](SECURITY.md)

© Amretra Dynamics.
