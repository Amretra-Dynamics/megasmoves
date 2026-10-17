import Link from "next/link";
import Image from "next/image";
import { ArrowRight, CheckCircle2 } from "lucide-react";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata={title:"Megas WoW India Pricing | MegasMoves",description:"Megas WoW India subscription, regular ₹1,999 per month, limited-time ₹999 per month."};
export default function Pricing(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-6xl px-6 py-16 md:py-24">
<p className="eyebrow">MEGAS WOW / INDIA</p>
<h1 className="mt-5 text-5xl font-extrabold tracking-[-0.06em] md:text-6xl">One subscription. <span className="text-[var(--accent)]">Your SI agent.</span></h1>
<p className="mt-6 text-lg text-[var(--text-soft)]">MegasMoves for your agents, tasks and compatible devices.</p>
<div className="mt-12 grid gap-10 md:grid-cols-2">
<section className="rounded-xl border border-[var(--line-strong)] bg-[var(--surface-1)] p-8 md:p-10">
<div className="flex items-center gap-4"><Image src="/megas-logo.png" alt="MegasMoves" width={50} height={50}/><h2 className="text-2xl font-bold">Megas WoW</h2></div>
<p className="mt-8 text-sm text-[var(--text-soft)]">Regular price <span className="line-through">₹1,999/month</span></p>
<p className="mt-2 text-5xl font-extrabold tracking-[-0.05em]">₹999 <span className="text-base font-normal text-[var(--text-soft)]">/ month</span></p>
<p className="mt-2 text-sm font-semibold text-[var(--accent)]">Limited-time India offer</p>
<p className="mt-6 text-sm leading-7 text-[var(--text-soft)]">The current ₹999/month subscription authorizes 12 monthly billing cycles. The standard price is ₹1,999/month; any further renewal or price change needs appropriate customer authorization. Applicable taxes, if any, are shown before payment.</p>
<a href="https://pay.megasmoves.io/" className="button button--primary mt-7 inline-flex items-center gap-2">Continue to secure checkout <ArrowRight className="h-4 w-4"/></a>
<p className="mt-4 text-sm"><a href="https://pay.megasmoves.io/account" className="underline underline-offset-4">Already subscribed? Manage billing</a></p>
<p className="mt-4 text-xs leading-6 text-[var(--muted)]">India only. Razorpay Test Mode is currently active. No real charge until live payments are enabled.</p>
</section>
<section className="py-5 text-sm leading-8 text-[var(--text-soft)]"><h2 className="text-xl font-bold text-[var(--text)]">Included with Megas WoW</h2>
{["Account-linked subscription and billing status","Compatible device and agent coordination","Shared Actions, task history and context"].map(t=><div key={t} className="mt-5 flex gap-3"><CheckCircle2 className="mt-1 h-5 w-5 shrink-0 text-[var(--accent)]"/>{t}</div>)}
<h3 className="mt-12 font-bold text-[var(--text)]">Subscription details</h3>
<p><Link href="/legal/subscription-terms/">Subscription terms</Link> · <Link href="/legal/offer-terms/">India offer</Link></p>
<p><Link href="/legal/cancellation/">Cancellation</Link> · <Link href="/legal/refunds/">Refunds</Link> · <Link href="/legal/service-privacy/">Service privacy</Link></p>
<p className="mt-4">Billing assistance: <a href="mailto:master@megasmoves.io">master@megasmoves.io</a></p>
</section></div></main><Footer/></div>}