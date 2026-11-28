import Link from "next/link";
import Image from "next/image";
import { ArrowRight, CheckCircle2 } from "lucide-react";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata={title:"Megas WoW Subscription | AI Agent Automation Pricing",description:"Review Razorpay test checkout, Dodo Payments billing setup, account requirements and subscription terms for Megas WoW.",alternates:{canonical:"/pricing/"}};
export default function Pricing(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-6xl px-6 py-16 md:py-24">
<p className="eyebrow">MEGAS WOW / CROSS-DEVICE AGENTS</p>
<h1 className="mt-5 text-5xl font-extrabold tracking-[-0.06em] md:text-6xl">One account. <span className="text-[var(--accent)]">Your agents and devices.</span></h1>
<p className="mt-6 text-lg text-[var(--text-soft)]">Keep AI agent activity, projects and approved local automation visible across Windows, Android, Linux and Mac, with one signed-in Megas account.</p>
<div className="mt-12 grid gap-10 md:grid-cols-2">
<section className="rounded-xl border border-[var(--line-strong)] bg-[var(--surface-1)] p-8 md:p-10">
<div className="flex items-center gap-4"><Image src="/megas-logo.png" alt="MegasMoves" width={50} height={50}/><h2 className="text-2xl font-bold">Megas WoW</h2></div>
<p className="mt-8 text-sm text-[var(--text-soft)]">Razorpay · India test subscription</p>
<p className="mt-2 text-5xl font-extrabold tracking-[-0.05em]">₹999 <span className="text-base font-normal text-[var(--text-soft)]">/ month</span></p>
<p className="mt-2 text-sm font-semibold text-[var(--accent)]">Test amount · no real charges</p>
<p className="mt-6 text-sm leading-7 text-[var(--text-soft)]">The Razorpay test checkout displays ₹999 per month for 12 billing cycles. No money is collected. This test configuration does not establish a live promotional offer.</p>
<a href="https://pay.megasmoves.io/checkout" className="button button--primary mt-7 inline-flex items-center gap-2">Open Razorpay test checkout <ArrowRight className="h-4 w-4"/></a>
<p className="mt-4 text-sm"><a href="https://pay.megasmoves.io/account" className="underline underline-offset-4">Sign in &amp; manage test billing</a></p>
<p className="mt-4 text-xs leading-6 text-[var(--muted)]">Sign in with a verified Megas account to continue. Live subscriptions are not available on this site. Any live price, taxes and renewal terms must be shown and accepted before a real purchase.</p>
<div className="mt-8 border-t border-[var(--line)] pt-6"><h3 className="text-lg font-bold">Dodo Payments</h3><p className="mt-2 text-sm font-semibold text-[var(--accent)]">Test billing setup in progress</p><p className="mt-3 text-sm leading-7 text-[var(--text-soft)]">Dodo checkout is not available on this site yet. Its payment option will be shown after the integration has been verified.</p></div>
</section>
<section className="py-5 text-sm leading-8 text-[var(--text-soft)]"><h2 className="text-xl font-bold text-[var(--text)]">Planned subscription scope</h2>
{["Account-linked subscription and billing status","Compatible device and agent coordination","Shared Actions, task history and context"].map(t=><div key={t} className="mt-5 flex gap-3"><CheckCircle2 className="mt-1 h-5 w-5 shrink-0 text-[var(--accent)]"/>{t}</div>)}
<h3 className="mt-12 font-bold text-[var(--text)]">Subscription details</h3>
<p><Link href="/legal/subscription-terms/">Subscription terms</Link> · <Link href="/legal/offer-terms/">India offer</Link></p>
<p><Link href="/legal/cancellation/">Cancellation</Link> · <Link href="/legal/refunds/">Refunds</Link> · <Link href="/legal/service-privacy/">Service privacy</Link></p>
<p className="mt-4">Billing assistance: <a href="mailto:master@megasmoves.io">master@megasmoves.io</a></p>
</section></div></main><Footer/></div>}