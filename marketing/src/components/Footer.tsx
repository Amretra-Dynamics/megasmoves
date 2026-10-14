import Image from "next/image";
import Link from "next/link";
import { ArrowUpRight, Building2, LockKeyhole, Mail, ShieldCheck } from "lucide-react";

const linkGroups = [
  {
    title: "Product",
    links: [
      { href: "/#product", label: "Megas Moves" },
      { href: "/#features", label: "Features" },
      { href: "/wow", label: "Megas WoW" },
      { href: "/download", label: "Download" },
      { href: "/android-testing", label: "Android testing" },
      { href: "https://github.com/Amretra-Dynamics/megasmoves", label: "GitHub source" },
      { href: "https://x.com/MegasMoves", label: "MegasMoves on X" },
    ],
  },
  {
    title: "Resources",
    links: [
      { href: "/docs", label: "Documentation" },
      { href: "/docs/getting-started", label: "Getting started" },
      { href: "/docs/cli-reference", label: "Agent reference" },
      { href: "/docs/product-plans", label: "Release status" },
      { href: "/oauth-app-info", label: "App information" },
    ],
  },
  {
    title: "Company",
    links: [
      { href: "/legal", label: "Legal center" },
      { href: "/privacy", label: "Privacy" },
      { href: "/legal/acceptable-use", label: "Trust and safety" },
      { href: "/download#platforms", label: "Platforms" },
      { href: "mailto:master@megasmoves.io", label: "Contact" },
    ],
  },
];

export default function Footer() {
  return (
    <footer className="mx-auto mt-14 w-[calc(100%_-_40px)] max-w-[1280px] border-t border-[var(--line)] text-[var(--text)] md:w-[calc(100%_-_56px)]">
      <div className="grid gap-px overflow-hidden rounded-b-lg border-x border-b border-[var(--line)] bg-[var(--line)] md:grid-cols-3" aria-label="Megas Moves trust commitments">
        <span className="flex items-center gap-3 bg-[var(--site-bg-raised)] px-5 py-4 text-xs font-semibold leading-5 text-[var(--text-soft)]"><ShieldCheck className="h-4 w-4 shrink-0 text-[var(--accent)]" aria-hidden="true" /> Local operation · authenticated remote connections</span>
        <span className="flex items-center gap-3 bg-[var(--site-bg-raised)] px-5 py-4 text-xs font-semibold leading-5 text-[var(--text-soft)]"><LockKeyhole className="h-4 w-4 shrink-0 text-[var(--accent)]" aria-hidden="true" /> Account controls for connected access</span>
        <span className="flex items-center gap-3 bg-[var(--site-bg-raised)] px-5 py-4 text-xs font-semibold leading-5 text-[var(--text-soft)]"><Building2 className="h-4 w-4 shrink-0 text-[var(--accent)]" aria-hidden="true" /> Operated by Amretra Dynamics, India</span>
      </div>

      <div className="grid gap-10 px-1 py-12 sm:grid-cols-2 lg:grid-cols-[1.35fr_repeat(3,0.7fr)_1.15fr] lg:gap-7 lg:px-3">
        <div className="sm:col-span-2 lg:col-span-1">
          <Link href="/" className="inline-flex items-center gap-3 no-underline" aria-label="Megas Moves home">
            <span className="flex h-10 w-10 items-center justify-center overflow-hidden rounded-lg bg-[#020a10]" aria-hidden="true"><Image src="/megas-logo-4k.png" alt="" width={44} height={44} className="h-full w-full scale-110 object-cover" /></span>
            <span className="text-lg font-extrabold">Megas Moves</span>
          </Link>
          <p className="mt-5 max-w-md text-sm leading-7 text-[var(--muted)]">One local operator for actions, tasks, runtime testing and approved agent operations.</p>
          <p className="mt-4 max-w-md text-xs leading-6 text-[var(--muted)]">A product of <strong className="text-[var(--text-soft)]">Amretra Dynamics</strong>, an MSME registered in India.</p>
        </div>

        {linkGroups.map((group) => (
          <nav key={group.title} aria-label={group.title} className="flex flex-col items-start gap-3">
            <h2 className="mb-1 text-xs font-extrabold uppercase tracking-[0.14em] text-[var(--text)]">{group.title}</h2>
            {group.links.map((link) =>
              link.href.startsWith("mailto:") ? (
                <a key={link.href} href={link.href} className="text-sm text-[var(--muted)] no-underline transition hover:text-[var(--accent)]">{link.label}</a>
              ) : (
                <Link key={link.href} href={link.href} className="text-sm text-[var(--muted)] no-underline transition hover:text-[var(--accent)]">{link.label}</Link>
              ),
            )}
          </nav>
        ))}

        <div className="sm:col-span-2 lg:col-span-1">
          <p className="flex items-center gap-2 text-xs font-extrabold uppercase tracking-[0.14em] text-[var(--accent)]"><Mail className="h-4 w-4" aria-hidden="true" /> Support and grievances</p>
          <h2 className="mt-4 text-2xl font-extrabold leading-tight">Legal, billing, privacy, or security question?</h2>
          <p className="mt-3 text-sm leading-7 text-[var(--muted)]">Include the app version and a brief description. Never send passwords or access tokens.</p>
          <a href="mailto:master@megasmoves.io" className="button button--primary mt-6">
            Contact Megas
            <ArrowUpRight aria-hidden="true" />
          </a>
        </div>
      </div>

      <section className="partner-enquiry" aria-labelledby="partner-title">
        <div><p className="eyebrow">Work with Megas Moves</p><h2 id="partner-title">Become a sales partner.</h2><p>Interested in introducing Megas Moves to your customers or community? Tell us about your audience and how you would like to work together.</p></div>
        <a className="button button--primary" href="mailto:master@megasmoves.io?subject=Megas%20Moves%20sales%20partnership%20enquiry">Email us about a partnership<ArrowUpRight aria-hidden="true" /></a>
      </section>
      <div className="border-t border-[var(--line)] px-3 py-5 text-xs leading-6 text-[var(--muted)]">
        Choose a preview build for your device. The ChatGPT plugin is coming soon.
      </div>

      <div className="flex flex-col gap-4 border-t border-[var(--line)] px-3 py-5 text-xs leading-5 text-[var(--muted)] lg:flex-row lg:items-center lg:justify-between">
        <span>{"\u00A9"} 2026 Amretra Dynamics. Megas Moves and associated branding. All rights reserved.</span>
        <nav aria-label="Legal" className="flex flex-wrap gap-x-5 gap-y-2">
          <Link href="/terms" className="no-underline hover:text-[var(--accent)]">Terms</Link>
          <Link href="/privacy" className="no-underline hover:text-[var(--accent)]">Privacy</Link>
          <Link href="/legal/refunds" className="no-underline hover:text-[var(--accent)]">Refunds</Link>
          <Link href="/legal/cancellation" className="no-underline hover:text-[var(--accent)]">Cancellation</Link>
          <Link href="/legal/cookies" className="no-underline hover:text-[var(--accent)]">Cookies</Link>
          <Link href="/legal" className="no-underline hover:text-[var(--accent)]">Legal center</Link>
        </nav>
      </div>
    </footer>
  );
}
