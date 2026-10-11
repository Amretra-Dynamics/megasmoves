import Link from "next/link";
import { ArrowRight, BookOpen, Terminal, ShieldCheck, CircleDollarSign } from "lucide-react";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = { title: "Documentation | MegasMoves", description: "MegasMoves Community setup, MCP connection, local permissions and current product plans." };
const guides = [
  { title: "Getting started", description: "Install the Community package and connect a compatible MCP client.", href: "/docs/getting-started", Icon: BookOpen },
  { title: "CLI and MCP", description: "The local entrypoint, capability contracts and security-first operation.", href: "/docs/cli-reference", Icon: Terminal },
  { title: "Plans", description: "Community availability and the planned Megas WoW Cloud experience.", href: "/docs/product-plans", Icon: CircleDollarSign },
];
export default function DocsHome() { return <div className="site-surface min-h-screen"><Header /><main className="mx-auto w-full max-w-6xl px-6 py-14 md:py-24">
  <p className="eyebrow"><ShieldCheck aria-hidden="true" /> MEGASMOVES / DOCUMENTATION</p>
  <h1 className="mt-5 max-w-5xl text-5xl font-extrabold tracking-[-0.06em] md:text-7xl">Build beyond the conversation.</h1>
  <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">MegasMoves connects compatible software agents to device and workspace operations through MCP and local permissions. Start with the Community release and explore the architecture.</p>
  <div className="mt-14 grid gap-5 md:grid-cols-3">{guides.map(({title,description,href,Icon})=><Link key={href} href={href} className="rounded-xl border border-[var(--line-strong)] bg-[var(--surface-1)] p-7 no-underline transition hover:border-[var(--accent)]"><Icon className="h-8 w-8 text-[var(--accent)]" aria-hidden="true" /><h2 className="mt-7 text-xl font-bold">{title}</h2><p className="mt-3 text-sm leading-7 text-[var(--muted)]">{description}</p><span className="mt-8 inline-flex items-center gap-2 text-xs font-bold uppercase text-[var(--accent)]">Read guide <ArrowRight className="h-4 w-4" /></span></Link>)}</div>
  <section className="mt-16 rounded-xl border border-[var(--line)] bg-[var(--surface-2)] p-7"><h2 className="text-2xl font-bold">A boundary worth keeping</h2><p className="mt-3 leading-7 text-[var(--text-soft)]">This public site is for documentation and product information. It does not host account authentication, private runtimes, or payment processing.</p></section>
</main><Footer /></div>; }