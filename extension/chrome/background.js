"use strict";

const HOST = "com.amretra.megasmoves";
const granted = new Map();
let port = null;
let reconnectTimer = null;
let online = false;

function supported(url) {
  try { return ["http:", "https:"].includes(new URL(url).protocol); }
  catch { return false; }
}
function originOf(url) {
  try { return new URL(url).origin; } catch { return ""; }
}
function connect() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST);
    online = true;
    port.onMessage.addListener(message => {
      if (message && message.kind === "command") handleCommand(message);
    });
    port.onDisconnect.addListener(() => {
      port = null;
      online = false;
      granted.clear(); // Fail closed whenever the local operator disconnects.
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 3000);
    });
  } catch {
    online = false;
    if (!reconnectTimer) reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, 3000);
  }
}
function emit(reply) {
  try { port?.postMessage(reply); } catch { /* extension disconnected */ }
}
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
connect();

chrome.tabs.onRemoved.addListener(id => granted.delete(id));
chrome.tabs.onUpdated.addListener((id, change) => {
  if (change.status === "loading" || change.url) granted.delete(id);
});

async function status() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return { connected: online, enabled: Boolean(tab && granted.has(tab.id)), tabId: tab?.id || null, url: tab?.url || "" };
}
chrome.runtime.onMessage.addListener((msg, sender, respond) => {
  (async () => {
    if (!msg || sender.id !== chrome.runtime.id) throw Error("Invalid extension request");
    if (msg.kind === "status") return status();
    if (msg.kind !== "enable" && msg.kind !== "disable") throw Error("Unsupported request");
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) throw Error("No active browser tab");
    if (msg.kind === "disable") { granted.delete(tab.id); return status(); }
    if (!online) throw Error("Local MegasMoves native host is not connected");
    if (!supported(tab.url)) throw Error("Only ordinary HTTP(S) tabs can be enabled");
    granted.set(tab.id, originOf(tab.url));
    return status();
  })().then(respond).catch(err => respond({ error: String(err?.message || err) }));
  return true;
});

async function allowedTab(id) {
  if (!Number.isSafeInteger(id) || !granted.has(id)) throw Error("Tab has not been enabled by the user");
  const tab = await chrome.tabs.get(id);
  if (!supported(tab.url) || granted.get(id) !== originOf(tab.url)) {
    granted.delete(id);
    throw Error("Tab permission expired after navigation");
  }
  return tab;
}
function pageObserve() {
  const elements = Array.from(document.querySelectorAll("a,button,input,textarea,select,[role=button]"))
    .filter(el => !!(el.getClientRects().length))
    .slice(0, 80).map(el => ({
      tag: el.tagName.toLowerCase(),
      role: (el.getAttribute("role") || "").slice(0, 40),
      label: (el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.innerText || "").trim().slice(0, 160),
      type: (el.getAttribute("type") || "").slice(0, 30)
    }));
  return { title: document.title.slice(0, 250), url: location.href.slice(0, 1600),
    text: (document.body?.innerText || "").slice(0, 12000), elements };
}
function pageClick(selector) {
  const node = document.querySelector(selector);
  if (!node || !node.getClientRects().length) throw Error("Visible target not found");
  const target = node.closest("button,a,input,[role=button]") || node;
  const words = [target.textContent, target.getAttribute("aria-label"), target.getAttribute("type"), target.getAttribute("value")].join(" ").toLowerCase();
  if (target.matches("input[type=password], input[type=file], [type=submit]") ||
      target.closest("form") && target.matches("button:not([type=button])") ||
      target.hasAttribute("download") ||
      /\b(pay|purchase|checkout|buy now|delete|remove account|transfer|send money|confirm order|submit|sign contract)\b/.test(words)) {
    throw Error("Sensitive action blocked: use Chrome directly");
  }
  target.click();
  return { clicked: true, tag: target.tagName.toLowerCase() };
}
function pageFill(selector, value) {
  const node = document.querySelector(selector);
  if (!node || !node.getClientRects().length) throw Error("Visible target not found");
  if (node.matches("input[type=password], input[type=file], input[type=hidden]") ||
      node.closest("[autocomplete=current-password], [autocomplete=new-password]")) {
    throw Error("Credential or file fields cannot be automated");
  }
  if (node.matches("input,textarea")) {
    if (node.disabled || node.readOnly) throw Error("Field is not editable");
    const proto = node.matches("textarea") ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (!setter) throw Error("Field cannot be filled");
    setter.call(node, value);
  } else if (node.isContentEditable) {
    node.textContent = value;
  } else throw Error("Target is not an editable field");
  node.dispatchEvent(new Event("input", { bubbles: true }));
  node.dispatchEvent(new Event("change", { bubbles: true }));
  return { filled: true };
}
async function injected(tabId, fn, args=[]) {
  const result = await chrome.scripting.executeScript({ target: { tabId }, func: fn, args });
  if (!result?.length) throw Error("No page execution result");
  return result[0].result;
}
async function handleCommand(message) {
  const id = message.id;
  try {
    const action = message.action;
    const tabId = message.tabId;
    let result;
    if (action === "tabs") {
      const tabs = await Promise.all(Array.from(granted.keys()).map(async key => {
        try {
          const tab = await allowedTab(key);
          return { tabId: key, title: (tab.title || "").slice(0, 200), url: tab.url };
        } catch { return null; }
      }));
      result = { tabs: tabs.filter(Boolean) };
    } else if (action === "status") {
      result = { connected: online, enabledTabs: granted.size };
    } else {
      const tab = await allowedTab(tabId);
      if (action === "observe") result = await injected(tabId, pageObserve);
      else if (action === "click") {
        if (typeof message.selector !== "string" || !message.selector.trim() || message.selector.length > 500)
          throw Error("Invalid CSS selector");
        result = await injected(tabId, pageClick, [message.selector]);
      } else if (action === "fill") {
        if (typeof message.selector !== "string" || !message.selector.trim() || message.selector.length > 500 ||
            typeof message.value !== "string" || message.value.length > 4000) throw Error("Invalid field data");
        result = await injected(tabId, pageFill, [message.selector, message.value]);
      } else if (action === "navigate") {
        if (typeof message.url !== "string" || !supported(message.url) || message.url.length > 2000 ||
            new URL(message.url).username || new URL(message.url).password) throw Error("Invalid navigation URL");
        granted.delete(tabId); // Navigation requires a new explicit grant.
        const moved = await chrome.tabs.update(tabId, { url: message.url });
        result = { navigated: true, url: moved.url || message.url, reenableRequired: true };
      } else if (action === "screenshot") {
        if (!tab.active) throw Error("Select the granted tab before capturing it");
        const image = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 45 });
        if (image.length > 650000) throw Error("Screenshot exceeds native message size limit");
        result = { dataUrl: image };
      } else throw Error("Unknown browser action");
    }
    emit({ kind: "result", id, ok: true, result });
  } catch (e) {
    emit({ kind: "result", id, ok: false, error: String(e?.message || e).slice(0,300) });
  }
}
