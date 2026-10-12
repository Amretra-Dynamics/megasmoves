# MegasMoves Chrome connector (Manifest V3)

This extension lets the **local** MegasMoves operator interact with tabs the person explicitly enabled. It does **not** use the dedicated Playwright browser profile, nor silently inherit its sessions.

## Install from a source checkout

1. Install the MegasMoves runtime on the computer running Chrome (`pip install megasmoves`). The extension and host must use matching product sources.
2. In Chrome open `chrome://extensions`, enable Developer mode, select **Load unpacked**, and choose the `extension/chrome` folder from the MegasMoves checkout.
3. Copy the extension ID shown by Chrome.
4. On **that same computer**, run `python -m megas.chrome_bridge install --extension-id YOUR_EXTENSION_ID`. This registers a per-user native-messaging host for that exact extension, not a wildcard.
5. Reload the extension in `chrome://extensions`. Open an ordinary website, click the MegasMoves extension and select **Enable this tab**.
6. Use a locally connected MegasMCP client's `chrome_control` tool, or test from the machine with `python -m megas.chrome_bridge status`, `tabs`, and `observe --tab-id N`.

The client may also invoke `click --tab-id N --selector 'button.open'`, `fill --tab-id N --selector 'input[name=q]' --value 'hello'`, `navigate --tab-id N --url 'https://example.com'`, or `screenshot --tab-id N`. The screenshot command is intended for short-lived local inspection and returns a data URL.

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
