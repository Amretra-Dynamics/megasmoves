"use client";
import { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";

type Platform = "Windows" | "macOS" | "Linux";
const pipCommand = "pip install megasmoves";

export default function TerminalInstall() {
  const [platform, setPlatform] = useState<Platform>("Windows");
  const [packageUrl, setPackageUrl] = useState("");
  const [packageFailed, setPackageFailed] = useState(false);
  const [copied, setCopied] = useState("");
  const [copyFailed, setCopyFailed] = useState(false);
  useEffect(() => {
    const device = navigator.userAgent;
    if (/Macintosh|Mac OS X/.test(device)) setPlatform("macOS");
    else if (/Linux/.test(device) && !/Android/.test(device)) setPlatform("Linux");
    const controller = new AbortController();
    const timeout = setTimeout(() => { setPackageFailed(true); controller.abort(); }, 10000);
    fetch("/python-package.json", { signal: controller.signal })
      .then(response => response.ok ? response.json() : null)
      .then(record => {
        if (record && ["public-release", "local-development-preview"].includes(record.scope)
          && /^megasmoves-[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.]+)?-py3-none-any\.whl$/.test(record.filename)
          && /^[a-f0-9]{64}$/.test(record.sha256)
          && Number.isSafeInteger(record.size_bytes) && record.size_bytes > 0 && record.size_bytes <= 50_000_000) {
          setPackageUrl(`https://megasmoves.io/downloads/${record.filename}`);
        } else throw new Error("Python package unavailable");
      }).catch(error => { if (error.name !== "AbortError") setPackageFailed(true); })
      .finally(() => clearTimeout(timeout));
    return () => { clearTimeout(timeout); controller.abort(); };
  }, []);
  const nativeCommand = platform === "Windows"
    ? "irm https://megasmoves.io/install.ps1 | iex"
    : "curl -fsSL https://megasmoves.io/install.sh | sh";
  async function copy(value: string, label: string) {
    try { await navigator.clipboard.writeText(value); setCopied(label); setCopyFailed(false); }
    catch { setCopyFailed(true); }
  }
  const command = (value: string, label: string) => <div className="terminal-install__command"><code>{value}</code><button type="button" className="button button--quiet" aria-label={`Copy ${label} command`} onClick={() => copy(value, label)}>{copied === label ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}{copied === label ? "Copied" : "Copy"}</button></div>;
  return <section id="terminal-install" className="terminal-install" aria-labelledby="terminal-install-title">
    <p className="eyebrow">Install your way</p><h3 id="terminal-install-title">App and CLI. One Megas Moves.</h3>
    <p>Already have Python? Use the same command on Windows, macOS and Linux.</p>
    {packageUrl ? command(`pip install ${packageUrl}`, "website package") : packageFailed ? <p role="alert">The Python download could not be loaded. Refresh the page or choose a native download above.</p> : <p role="status">Loading the Python download…</p>}
    <p className="artifact-note">Python 3.11 or later. Installs the package directly from Megas Moves.</p>
    <details className="package-install-option"><summary>PyPI installation · coming soon</summary>
      <p>The short command will be available when the package is published to PyPI.</p>
      <code>{pipCommand}</code>
    </details>
    <div className="native-install"><div className="native-install__heading"><h4>Without Python</h4><label>Choose your device<select value={platform} onChange={event => {setPlatform(event.target.value as Platform);setCopied("");}}><option>Windows</option><option>macOS</option><option>Linux</option></select></label></div>
      <p>{platform === "Windows" ? "Run in PowerShell. Downloads and verifies the Windows app installer, including the megas terminal command." : platform === "macOS" ? "Run in your Apple Silicon Mac terminal. Installs the command-line build with its bundled runtime." : "Run in your Linux x64 terminal. Installs the command-line build with its bundled runtime."}</p>
      {command(nativeCommand, platform)}
    </div>
    <p>After installation: <code>megas --help</code></p>
    {copyFailed ? <p role="alert">Copy is unavailable in this browser. Select the command above to copy it.</p> : null}
  </section>;
}
