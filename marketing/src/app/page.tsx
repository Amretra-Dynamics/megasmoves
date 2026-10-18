import type { Metadata } from "next";

export const metadata: Metadata = {
  alternates: { canonical: "/" },
};

const softwareSchema = {
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  name: "MegasMoves",
  alternateName: "Megas Moves",
  url: "https://megasmoves.io/",
  description:
    "Multi-device SI agent and local software automation platform with permission-aware MCP tools, browser operations and development workspaces.",
  applicationCategory: "DeveloperApplication",
  downloadUrl: "https://megasmoves.io/download/",
  creator: {
    "@type": "Organization",
    name: "Amretra Dynamics",
    url: "https://github.com/Amretra-Dynamics",
  },
  sameAs: [
    "https://github.com/Amretra-Dynamics/megasmoves",
    "https://x.com/MegasMoves",
  ],
};

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
import ProductFeatures from "@/components/ProductFeatures";
import HashNavigation from "@/components/HashNavigation";

const featureRail = [
  {
    icon: ScanSearch,
    title: "Coordinates your agents",
    copy: "Inspect supported agents, tasks and their recorded activity in the local operator.",
  },
  {
    icon: BellRing,
    title: "Notifies you when it matters",
    copy: "Review failures, pending work and evidence before choosing the next action.",
  },
  {
    icon: MessageSquareText,
    title: "Keeps work visible",
    copy: "Actions organize folder and file references with tasks in shared operator state.",
  },
  {
    icon: RefreshCw,
    title: "Shares one capability core",
    copy: "The Windows console and embedded MCP operate the same local capabilities.",
  },
];

const workflowSteps = [
  {
    number: "01",
    title: "Install",
    copy: "Download the build for your platform and review its verification status.",
    icon: Laptop2,
  },
  {
    number: "02",
    title: "Connect",
    copy: "Use the existing Firebase account flow inside the local app.",
    icon: Wifi,
  },
  {
    number: "03",
    title: "Choose work",
    copy: "Create an Action with file and folder references, then inspect tasks and runtime state.",
    icon: MessageSquareText,
  },
];

const tiers = [
  { name: "Windows", label: "Development build", description: "The native Windows console and embedded Megas MCP share one installation.", action: "Windows downloads", href: "/download", icon: Laptop2, accent: "cyan" },
  { name: "Android", label: "Preview APK", description: "Keep your connected agents and their work in view on your phone.", action: "Android download", href: "/download", icon: Smartphone, accent: "green" },
  { name: "Linux and macOS", label: "Platform-specific status", description: "Linux has an existing desktop development package. macOS command-line builds are listed by architecture on Downloads.", action: "Platform availability", href: "/download#platforms", icon: Terminal, accent: "violet" },
  { name: "ChatGPT plugin", label: "Coming soon", description: "Coming soon: connect ChatGPT to your local Megas Moves operator.", action: "Plugin status", href: "/download#chatgpt", icon: Bot, accent: "cyan" },
];

export default function Home() {
  return (
    <div className="landing-page">
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(softwareSchema) }} />
      <Header />
      <HashNavigation />

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
                Connect your agents to real tools. Keep Actions, tasks and context together, with a local operator that makes work visible.
              </p>
              <div className="velocity-hero__actions">
                <Link href="/download" className="button button--primary button--large">
                  Get started free
                  <ArrowRight aria-hidden="true" />
                </Link>
                <Link href="#features" className="button button--quiet button--large">
                  Explore the features
                  <ArrowRight aria-hidden="true" />
                </Link>
              </div>
              <Link href="/wow" className="hero-wow-link">Meet Megas WoW — your always-on SI agent <ArrowRight aria-hidden="true" /></Link>
            </div>

            <div className="velocity-hero__visual" aria-hidden="true">
              <span className="speed-line speed-line--one" />
              <span className="speed-line speed-line--two" />
              <span className="speed-line speed-line--three" />
              <span className="speed-node speed-node--one" />
              <span className="speed-node speed-node--two" />
              <span className="speed-node speed-node--three" />
              <div className="velocity-hero__mark">
                <Image src="/megas-logo-4k.png" alt="" fill priority sizes="(max-width: 900px) 92vw, 54vw" />
              </div>
            </div>
          </div>

          <div className="feature-rail" aria-label="Megas Moves capabilities">
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

        <ProductFeatures />

        <section id="product" className="system-contrast reveal-section" aria-labelledby="system-title">
          <h2 id="system-title" className="sr-only">One operator for people and AI clients</h2>
          <div className="system-contrast__grid">
            <article className="contrast-panel contrast-panel--problem">
              <div className="contrast-panel__copy">
                <p className="eyebrow">Your tools</p>
                <h3>Bring the work together.</h3>
                <p>Local services, selected files and connected devices, organized around the task at hand.</p>
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
                <p className="eyebrow">With Megas Moves</p>
                <h3>One operator. Shared context.</h3>
                <p>You use the desktop. Your agents use Megas MCP. Both work with the same Actions and tasks.</p>
              </div>
              <div className="context-stack" aria-hidden="true">
                <span className="context-layer context-layer--back" />
                <span className="context-layer context-layer--middle" />
                <span className="context-layer context-layer--front">
                  <Image src="/megas-logo-4k.png" alt="" width={76} height={76} />
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
            <h2 id="workflow-title">From setup to your next action, <strong>with visible evidence.</strong></h2>
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
            <p className="eyebrow">Across your devices</p>
            <h2 id="android-title">Your work.<br />Within reach.</h2>
            <p>Download a preview build for Windows, Android or Linux, or the macOS Apple Silicon command-line package. The ChatGPT plugin is coming soon.</p>
            <div className="android-band__actions">
              <Link href="/download" className="button button--primary">Android downloads <ArrowRight aria-hidden="true" /></Link>
              <Link href="/docs/getting-started" className="text-link">Setup documentation <ArrowRight aria-hidden="true" /></Link>
            </div>
          </div>
          <div className="phone-stage" aria-hidden="true">
            <div className="phone-shell">
              <span className="phone-shell__speaker" />
              <div className="phone-shell__screen"><Image src="/megas-logo-4k.png" alt="" fill sizes="260px" /><span>Illustrative preview</span></div>
            </div>
          </div>
          <ol className="android-path">
            <li><span><Laptop2 aria-hidden="true" /></span><div><strong>Detect</strong><p>Megas checks supported agents on your connected computer.</p></div></li>
            <li><span><Bot aria-hidden="true" /></span><div><strong>Choose</strong><p>Available agents appear in Android and the web console.</p></div></li>
            <li><span><BellRing aria-hidden="true" /></span><div><strong>Stay informed</strong><p>Follow the stream or leave the app and return when an agent notifies you.</p></div></li>
          </ol>
        </section>

        <section id="plans" className="plans-band reveal-section" aria-labelledby="plans-title">
          <div className="section-heading plans-band__heading">
            <p className="eyebrow">Platform availability</p>
            <h2 id="plans-title">Get the right build for your device.</h2>
            <Link href="/download" className="text-link">See available downloads <ArrowRight aria-hidden="true" /></Link>
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
            <span><CheckCircle2 aria-hidden="true" /> Development builds clearly labelled</span>
            <span><CheckCircle2 aria-hidden="true" /> File sizes and SHA-256 checksums</span>
            <span><CheckCircle2 aria-hidden="true" /> Public ChatGPT plugin coming soon</span>
          </div>
        </section>
      </main>
      <Footer />
    </div>
  );
}
