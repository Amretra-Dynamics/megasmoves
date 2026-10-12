# MegasMoves Chrome connector (Manifest V3)

This extension lets the **local** MegasMoves operator interact with tabs the person explicitly enabled. It does **not** use the dedicated Playwright browser profile, nor silently inherit its sessions.

## Install the downloadable extension ZIP

1. Download the `megasmoves-chrome-extension` artifact from the successful [Chrome extension workflow](https://github.com/Amretra-Dynamics/megasmoves/actions/workflows/chrome-extension-ci.yml). Extract the ZIP to a **permanent folder** on the computer running Chrome. The archive contains the extension and its standalone Python bridge.
2. Install Python 3.11+ if needed.
3. Open `chrome://extensions` and turn on **Developer mode**. Choose **Load unpacked** and select the extracted folder containing `manifest.json`.
4. Copy the extension ID shown on the Chrome extensions screen.
5. In a terminal in the extracted folder, run `python chrome_bridge.py install --extension-id YOUR_EXTENSION_ID`. This registers a per-user native host for this exact Chrome extension ID.
6. Reload the extension in `chrome://extensions`. Visit an ordinary HTTP(S) website. Open the MegasMoves extension popup and select **Enable this tab**.
7. On the same computer run `python chrome_bridge.py status`, then `python chrome_bridge.py tabs` and `python chrome_bridge.py observe --tab-id N` where N is the enabled tab ID.

For local interaction use `python chrome_bridge.py click --tab-id N --selector 'button.open'`, `fill --tab-id N --selector 'input[name=q]' --value 'hello'`, `navigate --tab-id N --url 'https://example.com'`, or `screenshot --tab-id N`. Navigation revokes the grant: re-enable the tab afterward. The screenshot command returns a data URL, so prefer a client that supports image output.

When the matching MegasMoves runtime build is installed, connected MCP clients can also use its `chrome_status` and `chrome_control` tools. MegasMCP write operations retain the local **Megas Operate enabled** requirement. An older MegasMoves pip release will not contain these new MCP tools until a matching runtime release is installed.

**Important:** Do not relocate or delete the extracted extension/bridge folder after registering the host; the native host launcher references that exact path. It is possible to re-register after moving it. The bundled Python script uses the Python standard library and requires no separate remote cloud service.

## Permissions and boundaries

- `activeTab` and `scripting`: page access only after an explicit click on the extension for the active tab. No persistent all-sites host permission or content scripts.
- `nativeMessaging`: scoped to a host registered for the specific Chrome extension ID.
- The native host is started by Chrome; it opens an ephemeral HTTP command port **only on 127.0.0.1**, with a new random bearer token held in `~/.megas/chrome/bridge.json` (Windows: `%LOCALAPPDATA%/Amretra/MegasMoves/chrome/bridge.json`). The file is user-restricted, and the HTTP handler rejects requests carrying a browser `Origin` header.
- The tab grant is held in memory. It is revoked on navigation, tab close, or native-host disconnection. Navigation requests require the person to re-enable the destination tab.
- Password and file input fields, form submissions, and obviously destructive button labels are blocked. **This is not a guarantee** that arbitrary website actions are safe. Never grant a tab for financial, administrative, or sensitive accounts unless you intend to supervise it.
- No remote listener, extension analytics, cookie access, secret synchronization, arbitrary JavaScript evaluation, or unattended blanket tab control.
- Megas Operate must be locally enabled to run browser write commands through MegasMCP.

## Current boundaries

Chrome's internal pages and other extensions cannot be controlled. This initial connector targets normal websites and the currently visible tab for screenshots. It is a source-installed engineering preview, **not** a Chrome Web Store release and not proof of end-to-end testing on Windows/macOS/Linux. Native desktop packaging and signed browser-store distribution are future steps.

See `worker/megas/chrome_bridge.py` for host registration and the local connector protocol.
