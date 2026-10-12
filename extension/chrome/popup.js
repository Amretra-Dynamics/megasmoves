"use strict";
const bridge = document.getElementById("bridge");
const tabLabel = document.getElementById("tab");
const toggle = document.getElementById("toggle");
const errorLabel = document.getElementById("error");
let enabled = false;
async function send(kind) {
  const result = await chrome.runtime.sendMessage({kind});
  if (!result || result.error) throw Error(result?.error || "Extension is unavailable");
  return result;
}
function render(state) {
  enabled = !!state.enabled;
  bridge.textContent = state.connected ? "Local MegasMoves connected" : "Local host not connected";
  bridge.className = "status " + (state.connected ? "ok" : "off");
  tabLabel.textContent = state.url || "Open an HTTP(S) tab";
  toggle.textContent = enabled ? "Disable this tab" : "Enable this tab";
  toggle.className = enabled ? "active" : "";
  toggle.disabled = !state.connected || (!enabled && !/^https?:\/\//.test(state.url));
}
async function refresh() { try { render(await send("status")); } catch(e) { errorLabel.textContent = e.message; } }
toggle.addEventListener("click", async () => {
  toggle.disabled = true; errorLabel.textContent = "";
  try { render(await send(enabled ? "disable" : "enable")); }
  catch(e) { errorLabel.textContent = e.message; }
  finally { refresh(); }
});
refresh();
