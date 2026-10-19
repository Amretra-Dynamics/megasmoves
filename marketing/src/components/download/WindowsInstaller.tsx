"use client";
import { useEffect, useState } from "react";
import { Download, Laptop2, Smartphone, Apple, Terminal } from "lucide-react";
type Artifact = { filename: string; title: string; version: string; status: string; size_bytes: number; sha256: string; href: string; source_updated_at?: string | null; built_at?: string | null; source_revision?: string; source_note?: string };
export default function WindowsInstaller() {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [local, setLocal] = useState(false);
  const [published, setPublished] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => { if (artifacts.length) window.dispatchEvent(new Event("megas:downloads-ready")); }, [artifacts]);
  useEffect(() => {
    setLocal(["localhost", "127.0.0.1", "[::1]"].includes(location.hostname));
    const controller = new AbortController();
    const timeout = setTimeout(() => { setFailed(true); controller.abort(); }, 10000);
    fetch("/downloads.json", { cache: "no-store", signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error("Catalog unavailable"); return response.json(); })
      .then(catalog => {
        if (!["local-development-preview", "public-release"].includes(catalog.scope) || !Array.isArray(catalog.artifacts)) throw new Error("Unverified catalog");
        setPublished(catalog.scope === "public-release");
        const verified = catalog.artifacts.filter((item: Artifact) => item && typeof item.filename === "string" && /^[\w.-]+$/.test(item.filename) && /^[a-f0-9]{64}$/.test(item.sha256) && Number.isSafeInteger(item.size_bytes) && item.size_bytes > 0 && (item.href === `./downloads/${item.filename}` || (typeof item.href === "string" && /^https:\/\/github\.com\/Amretra-Dynamics\/megasmoves\/releases\/download\/[\w.-]+\/[\w.-]+$/.test(item.href) && item.href.endsWith(`/${item.filename}`))));
        if (!verified.length) throw new Error("No verified downloads");
        setArtifacts(verified);
      }).catch(error => { if (error.name !== "AbortError") setFailed(true); })
      .finally(() => clearTimeout(timeout));
    return () => { clearTimeout(timeout); controller.abort(); };
  }, []);
  const platforms = [
    { name: "Windows", match: "Windows installer", icon: Laptop2, format: "Windows x64 · Installer", copy: "The complete desktop app with embedded Megas MCP." },
    { name: "Android", match: "Android APK", icon: Smartphone, format: "Android · APK", copy: "Take Megas Moves with you on your phone." },
    { name: "macOS", match: "macOS CLI (Apple Silicon)", icon: Apple, format: "Apple Silicon · Command line", copy: "The retained command-line build for Apple Silicon." },
    { name: "Linux", match: "Linux native runtime", icon: Terminal, format: "Linux x64 · Command line", copy: "The command-line build with its bundled runtime." },
  ];
  const download = (item: Artifact) => published
    ? <a className="button button--primary" href={item.href}><Download aria-hidden="true" />Download</a>
    : local
    ? <a className="button button--primary" href={`http://127.0.0.1:3106/megasmoves/downloads/${encodeURIComponent(item.filename)}`}><Download aria-hidden="true" />Download</a>
    : <span className="artifact-note">Public download coming soon</span>;
  const date = (value: string) => {
    const parsed = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? value + "T00:00:00Z" : value);
    return Number.isNaN(parsed.getTime()) ? "Not recorded" : parsed.toLocaleDateString("en-GB", {day:"numeric",month:"short",year:"numeric",timeZone:"UTC"});
  };
  const verification = (item: Artifact) => <details className="artifact-verification"><summary>File details &amp; SHA-256</summary><p>{item.status}</p><p>{item.filename}</p>{item.source_revision ? <p>Source: {item.source_revision}</p> : null}{item.source_note ? <p>{item.source_note}</p> : null}<code>{item.sha256}</code></details>;
  return <div className="platform-downloads">
    {failed ? <p role="alert">Downloads could not be loaded. Please refresh the page.</p> : null}
    {!failed && !artifacts.length ? <p role="status">Loading downloads…</p> : null}
    <div className="platform-download-grid">{platforms.map(({name,match,icon:Icon,format,copy}) => {
      const item=artifacts.find(row=>row.title===match);
      return <article key={name} className="platform-download">
        <div className="platform-download__heading"><Icon aria-hidden="true"/><h3>{name}</h3><span>Preview</span></div>
        <p className="platform-download__format">{format}</p><p className="platform-download__copy">{copy}</p>
        {item ? <><div className="platform-download__action">{download(item)}<span>v{item.version}<br/>{(item.size_bytes/1024/1024).toFixed(1)} MB</span></div><dl className="artifact-dates"><div><dt>Code updated</dt><dd>{item.source_updated_at ? date(item.source_updated_at) : "Not recorded"}</dd></div>{item.built_at ? <div><dt>Build date</dt><dd>{date(item.built_at)}</dd></div> : null}</dl>{verification(item)}</> : <p className="artifact-note">{failed ? 'Unavailable' : artifacts.length ? 'No download listed for this platform.' : 'Loading…'}</p>}
      </article>;
    })}</div>
    <details className="additional-downloads"><summary>Windows application · Direct executable</summary>
      {artifacts.filter(item=>item.title==='Windows application').map(item=><article key={item.filename}><div><h3>{item.title}</h3><p>v{item.version} · {(item.size_bytes/1024/1024).toFixed(1)} MB</p></div>{download(item)}{verification(item)}</article>)}
    </details>
    <p className="artifact-note">Preview builds. Windows downloads are unsigned; the Android APK is debug-signed.</p>
  </div>;
}
