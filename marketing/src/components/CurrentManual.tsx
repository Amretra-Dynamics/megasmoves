import Link from "next/link";
const guides = {
  overview: { title: "Megas Moves documentation", sections: [
    ["One local operator", "MegasMoves.exe is the complete Windows product: native console, local runtime and embedded Megas MCP. People and agents use the same capability core. The marketing website does not operate your computer."],
    ["Actions and tasks", "Create and edit multiple Actions with folder references, file references and tasks. Selecting a path records shared context; it does not start services. Chitraagni workspaces, projects and products remain in the authorized integration."],
    ["Readiness and evidence", "Inspect supported agents, selected services, ports, bounded logs, connected devices and recorded operations. Start only the selected service slice. Confirm the observed outcome before marking work complete."],
    ["Availability", "Development downloads include Windows, Android and a bundled Linux command-line build. macOS command-line builds are listed by architecture on Downloads. The public ChatGPT plugin is coming soon."],
  ] },
  setup: { title: "Getting started", sections: [
    ["Choose a build", "Open Downloads and check platform, version, code revision date, build date, size and SHA-256. The Windows installer includes the complete app and the megas terminal launcher."],
    ["Install with Python", "The package command is pip install megasmoves on Windows, macOS and Linux with Python 3.11 or later. It provides the megas launcher. On Windows the first run installs the native app if needed, then uses that same installation. Use the direct package command on Downloads while PyPI publication is pending."],
    ["Install from our domain", "Use the Python-package command on Downloads to install directly from megasmoves.io. It does not require Git or a PyPI listing. Native Windows and macOS/Linux commands use megasmoves.io/install.ps1 and megasmoves.io/install.sh. The scripts verify each download before installation."],
    ["Install without Python", "Select your operating system under Without Python on Downloads. Windows uses PowerShell to verify and run the app installer. macOS and Linux use the bundled native runtime with curl and system checksum tools. macOS architecture and release version must match your device. Account credentials and saved work are preserved."],
    ["Update in place", "Install over the existing MegasMoves installation and preserve credentials and saved data. Avoid running an old installed copy alongside a repository build. Unsigned development builds are not signed production updates."],
    ["Sign in locally", "Use the existing Firebase account flow inside the application. The marketing website is not a prerequisite for local operation. Never put passwords or access tokens in agent prompts."],
    ["Choose work", "Create an Action, select its folder and file references, and add tasks. This is the shared registry visible to people and agents. Saving an Action does not execute it."],
    ["Inspect and connect", "Check readiness, ports, logs and devices. Use the application embedded MCP setup and verify a real tool call against the installed operator. Private testing is separate from public ChatGPT publication."],
  ] },
  agents: { title: "Agent and integration reference", sections: [
    ["Discover the live contract", "Read the installed MCP schema and server metadata. Do not reuse stale tool names or claim success for unavailable capabilities. Actions, tasks, sessions and evidence share one operator."],
    ["Bounded local operation", "Use typed check, status, ports, start, stop, restart and bounded logs operations. Executable and secret references are machine configuration. Arbitrary shell commands, database queries and ADB shell input are not exposed."],
    ["Chrome connector", "Tab discovery, observation, activation, navigation, click, fill, screenshot and clean-tab closure are supported. Reuse tabs and refresh IDs after closure. Edited forms are protected. A click is not evidence of submission."],
    ["Browser limits", "Credential and file fields, browser-internal pages and native sign-in prompts remain protected. DOM clicks may be blocked from opening popups. Dedicated browser-runtime upload is separate from Chrome-connector upload. No universal Google sign-in or upload guarantee is made."],
    ["Authorized integrations", "Chitraagni uses reviewed mounted APIs with pinned identity and backend authorization. Remote MCP authenticates and routes to the same account-owned installed operator. Credentials stop at their transport boundary."],
  ] },
  availability: { title: "Release and access status", sections: [
    ["Development downloads", "Windows and Android development files and a bundled Linux command-line build are listed with hashes and sizes. Choose a package matching your operating system and architecture."],
    ["Public release", "Preview downloads are available. Windows preview builds are unsigned. Paid subscriptions are not offered on this site."],
    ["macOS", "The retained Apple Silicon command-line build is available on Downloads. Choose the architecture shown on the package."],
    ["ChatGPT plugin", "Coming soon. Private integration tests and private tool calls do not mean the public plugin is available to everyone."],
  ] },
};
export default function CurrentManual({ guide }: { guide: keyof typeof guides }) {
  const content = guides[guide];
  return <article className="manual-page"><p className="eyebrow">Megas Moves · Updated 14 October 2026</p><h1>{content.title}</h1>
    {content.sections.map(([heading, body]) => <section key={heading}><h2>{heading}</h2><p>{body}</p></section>)}
    <nav aria-label="Manual navigation"><Link href="/docs">Manual</Link><Link href="/docs/getting-started">Getting started</Link><Link href="/docs/cli-reference">Agent reference</Link><Link href="/docs/product-plans">Release status</Link><Link href="/download">Downloads</Link><a href="https://github.com/Amretra-Dynamics/megasmoves">GitHub source</a></nav>
  </article>;
}
