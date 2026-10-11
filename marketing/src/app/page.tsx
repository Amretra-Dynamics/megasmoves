import Image from "next/image";
import Link from "next/link";
import {
  ArrowRight,
  Bot,
  CheckCircle2,
  Laptop2,
  MessageSquareText,
  BellRing,
  RefreshCw,
  ScanSearch,
  Smartphone,
  Terminal,
  Wifi,
  Zap,
  Radar,
  Workflow,
  ShieldCheck,
} from "lucide-react";
import Footer from "@/components/Footer";
import Header from "@/components/Header";

const featureRail = [
  {
    icon: ScanSearch,
    title: "Typed MCP interfaces",
    copy: "Compatible clients request bounded actions through the MegasMCP operator.",
  },
  {
    icon: BellRing,
    title: "Operates near your tools",
    copy: "Local runtimes operate with their own device and workspace authority.",
  },
  {
    icon: MessageSquareText,
    title: "Connected device surfaces",
    copy: "Windows, Android and browser workflows are part of the MegasMoves system.",
  },
  {
    icon: RefreshCw,
    title: "Permissions come first",
    copy: "Execution is governed by scoped access and human approval where required.",
  },
];

const workflowSteps = [
  {
    number: "01",
    title: "Install",
    copy: "Install the Community package on your supported computer.",
    icon: Laptop2,
  },
  {
    number: "02",
    title: "Connect",
    copy: "Connect a compatible MCP client to the local Megas interface.",
    icon: Wifi,
  },
  {
    number: "03",
    title: "Operate",
    copy: "Request supported actions, review permissions, and inspect the result.",
    icon: MessageSquareText,
  },
];

const tiers = [
  { name: "Community", label: "Open source · available", description: "MIT-licensed Community source, a local CLI, and an MCP interface to explore.", action: "Explore Community", href: "/download", icon: Bot, accent: "cyan" },
  { name: "Megas WoW", label: "$20 / month · planned", description: "Your always-on SI agent for all your devices. Managed service availability will be announced.", action: "Discover Megas WoW", href: "/pricing", icon: Radar, accent: "violet" },
  { name: "Documentation", label: "Get started", description: "Understand the CLI, MCP entrypoint, security boundaries and supported workflows.", action: "Read documentation", href: "/docs", icon: Workflow, accent: "green" },
  { name: "Community", label: "Contribute", description: "Inspect the public project, propose improvements and share real-world feedback.", action: "Explore the source", href: "https://github.com/Amretra-Dynamics/megasmoves", icon: ShieldCheck, accent: "orange" },
];

export default function Home() {
  return (
    <div className="landing-page">
      <Header />

      <main>
        <section className="velocity-hero" aria-labelledby="hero-title">
          <div className="velocity-hero__inner">
            <div className="velocity-hero__copy">
              <p className="eyebrow hero-eyebrow">
                <Zap aria-hidden="true" />
                Real execution for SI agents
              </p>
              <h1 id="hero-title" className="velocity-hero__title">
                <span className="velocity-hero__promise">
                  Your SI agent.
                  <br />
                  <strong>Across your devices.</strong>
                </span>
              </h1>
              <p className="velocity-hero__lede">
                MegasMoves connects compatible software agents with device and workspace capabilities through MCP, local runtimes, and permission-aware operation.
              </p>
              <div className="velocity-hero__actions">
                <Link href="/download" className="button button--primary button--large">
                  Explore Community
                  <ArrowRight aria-hidden="true" />
                </Link>
                <Link href="/docs" className="button button--quiet button--large">
                  Explore the docs
                  <ArrowRight aria-hidden="true" />
                </Link>
              </div>
              <p className="text-sm text-slate-500">Community source is available now. Megas WoW is planned, with no checkout yet.</p>
            </div>

            <div className="velocity-hero__visual" aria-hidden="true">
              <span className="speed-line speed-line--one" />
              <span className="speed-line speed-line--two" />
              <span className="speed-line speed-line--three" />
              <span className="speed-node speed-node--one" />
              <span className="speed-node speed-node--two" />
              <span className="speed-node speed-node--three" />
              <div className="velocity-hero__mark">
                <Image src="/megasmoves/megas-mark.svg" alt="" fill priority sizes="(max-width: 900px) 92vw, 54vw" />
              </div>
            </div>
          </div>

          <div className="feature-rail" aria-label="MegasMoves capabilities">
            {featureRail.map((feature) => {
              const Icon = feature.icon;
              return (
                <article key={feature.title}>
                  <Icon aria-hidden="true" />
                  <div><h2>{feature.title}</h2><p>{feature.copy}</p></div>
                </article>
              );
            })}
          </div>
        </section>

        <section id="product" className="system-contrast reveal-section" aria-labelledby="system-title">
          <h2 id="system-title" className="sr-only">Connect agents to authorized device operations</h2>
          <div className="system-contrast__grid">
            <article className="contrast-panel contrast-panel--problem">
              <div className="contrast-panel__copy">
                <p className="eyebrow">Before Megas</p>
                <h3>Agents need access to real software.</h3>
                <p>Useful actions span browsers, operating systems, files, and connected environments.</p>
              </div>
              <div className="scatter-visual" aria-hidden="true">
                <span className="scatter-orbit" />
                <span className="context-chip context-chip--one"><Terminal /> Agent</span>
                <span className="context-chip context-chip--two"><Laptop2 /> PC</span>
                <span className="unknown-node unknown-node--one">?</span>
                <span className="unknown-node unknown-node--two">?</span>
              </div>
            </article>

            <article className="contrast-panel contrast-panel--solution">
              <div className="contrast-panel__copy">
                <p className="eyebrow">With MegasMoves</p>
                <h3>Your devices become capable tools.</h3>
                <p>Give agents typed access to supported operations, scoped permissions, and observable results.</p>
              </div>
              <div className="context-stack" aria-hidden="true">
                <span className="context-layer context-layer--back" />
                <span className="context-layer context-layer--middle" />
                <span className="context-layer context-layer--front">
                  <Image src="/megasmoves/megas-mark.svg" alt="" width={76} height={76} />
                </span>
                <span className="stack-tool stack-tool--code"><Terminal /></span>
                <span className="stack-tool stack-tool--terminal"><MessageSquareText /></span>
                <span className="stack-tool stack-tool--people"><Smartphone /></span>
              </div>
            </article>
          </div>
        </section>

        <section id="workflow" className="workflow-band reveal-section" aria-labelledby="workflow-title">
          <div className="workflow-band__intro">
            <p className="eyebrow">Three simple steps</p>
            <h2 id="workflow-title">From an agent request to <strong>permission-controlled execution.</strong></h2>
          </div>
          <ol className="workflow-steps">
            {workflowSteps.map((step, index) => {
              const Icon = step.icon;
              return (
                <li key={step.number}>
                  <div className="workflow-step__topline"><span>{step.number}</span><h3>{step.title}</h3></div>
                  <div className="workflow-step__body"><span aria-hidden="true"><Icon /></span><p>{step.copy}</p></div>
                  {index < workflowSteps.length - 1 ? <ArrowRight className="workflow-step__arrow" aria-hidden="true" /> : null}
                </li>
              );
            })}
          </ol>
        </section>

        <section className="android-band reveal-section" aria-labelledby="android-title">
          <div className="android-band__copy">
            <p className="eyebrow">Windows + Android + MCP + browser workflows</p>
            <h2 id="android-title">Give compatible agents a path to real actions.</h2>
            <p>Bring software agents closer to the tools you use, with explicit capabilities and device-side execution.</p>
            <div className="android-band__actions">
              <Link href="/docs/getting-started" className="button button--primary">Read setup guide <ArrowRight aria-hidden="true" /></Link>
              <Link href="https://github.com/Amretra-Dynamics/megasmoves" className="text-link">Explore the source <ArrowRight aria-hidden="true" /></Link>
            </div>
          </div>
          <div className="phone-stage" aria-hidden="true">
            <div className="phone-shell">
              <span className="phone-shell__speaker" />
              <div className="phone-shell__screen"><Image src="/megasmoves/megas-mark.svg" alt="" fill sizes="260px" /><span>SI agent interface</span></div>
            </div>
          </div>
          <ol className="android-path">
            <li><span><Laptop2 aria-hidden="true" /></span><div><strong>Detect</strong><p>Megas exposes supported local capabilities through typed MCP interfaces.</p></div></li>
            <li><span><Bot aria-hidden="true" /></span><div><strong>Authorize</strong><p>Only granted scopes and supported operations are eligible to run.</p></div></li>
            <li><span><BellRing aria-hidden="true" /></span><div><strong>Inspect</strong><p>Review execution state, results, and the controls that keep you in charge.</p></div></li>
          </ol>
        </section>

        <section id="plans" className="plans-band reveal-section" aria-labelledby="plans-title">
          <div className="section-heading plans-band__heading">
            <p className="eyebrow">Community and managed experience</p>
            <h2 id="plans-title">Open source today. An always-on SI agent experience next.</h2>
            <Link href="/pricing" className="text-link">Explore plans <ArrowRight aria-hidden="true" /></Link>
          </div>
          <div className="plan-grid">
            {tiers.map((tier) => {
              const Icon = tier.icon;
              return (
                <article key={tier.name} className={`plan-card plan-card--${tier.accent}`}>
                  <div className="plan-card__topline"><Icon aria-hidden="true" /><span>{tier.label}</span></div>
                  <h3>{tier.name}</h3>
                  <p>{tier.description}</p>
                  <Link href={tier.href} aria-label={tier.action}><ArrowRight aria-hidden="true" /></Link>
                </article>
              );
            })}
          </div>
          <div className="plan-proof">
            <span><CheckCircle2 aria-hidden="true" /> Community source is MIT licensed</span>
            <span><CheckCircle2 aria-hidden="true" /> Permission-scoped device operations</span>
            <span><CheckCircle2 aria-hidden="true" /> Megas WoW is planned; checkout is not live</span>
          </div>
        </section>
      </main>
      <Footer />
    </div>
  );
}
