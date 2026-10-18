<p align="center">
  <img src="worker/windows/MegasMoves.WinUI/Assets/app.png" alt="MegasMoves logo" width="128" height="128">
</p>

# MegasMoves | Open-Source SI Agent & Multi-Device Automation

**Your always-on SI agent for all your devices.**

MegasMoves (also called Megas Moves) is an open-source **SI agent platform** and **developer automation runtime** that connects compatible agents to local tools, trusted workspaces, browser tabs and device-specific operations using the **Model Context Protocol (MCP)**. It is designed around observable execution, explicit permissions and human-visible work. The public builds and integrations are an evolving engineering preview.

**[Website](https://megasmoves.io/)** · **[Installation & downloads](https://megasmoves.io/download/)** · **[Getting started](https://megasmoves.io/docs/getting-started/)** · **[MCP / CLI documentation](https://megasmoves.io/docs/cli-reference/)** · **[Security policy](SECURITY.md)**


**MegasMoves** by **Amretra Dynamics** is a multi-device software automation platform built around native applications, a local execution runtime, the Model Context Protocol (MCP), and explicit device and workspace permissions.

**Community:** [Join MegasMoves on Telegram](https://t.me/megasmovesSI) · [Follow on X](https://x.com/MegasMoves) · [Website](https://megasmoves.io/)

## What you can explore

- **Local tools and runtimes:** inspect developer workspaces, processes, service readiness and logs through typed interfaces.
- **MCP integrations:** give compatible agent clients explicitly scoped operator capabilities, not unrestricted machine access.
- **Browser automation:** interact with approved Chrome tabs via an opt-in extension, with operation boundaries and observable outcomes.
- **Cross-platform engineering:** Windows native operator, Android client and platform-specific macOS/Linux components.
- **Work continuity:** organize actions, file references, tasks and handoffs through the same operator.

The release status and supported functions vary by operating system. Check [Downloads](https://megasmoves.io/download/) and [documentation](https://megasmoves.io/docs/) before depending on a capability.

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

## Chrome browser connector

MegasMoves includes an opt-in Manifest V3 Chrome extension for controlling explicitly enabled tabs in an existing Chrome window. Unlike the dedicated Megas Browser, the extension uses a local native-messaging host and grants access one tab at a time. See [Chrome connector installation and safety](extension/chrome/README.md). It is currently a source-installed engineering preview.

## Developer interface

The CLI entrypoint is **`megas`**. The Python distribution uses the name **`megasmoves`**.

```sh
pip install https://megasmoves.io/downloads/megasmoves-0.5.0-py3-none-any.whl
megas connect
```

The published Python package provides the `megas` command. On Windows it delegates
to the same installed MegasMoves application, installing it on first use if needed.
The short `pip install megasmoves` command is pending PyPI publication.

Visit [Downloads](https://megasmoves.io/download/) for native installation commands,
preview Windows/Android downloads, retained macOS/Linux CLI builds, versions and
checksums. These preview artifacts are not a signed production release.

Website updates are built locally and published using the
[website deployment guide](.github/WEBSITE_DEPLOYMENT.md).

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
