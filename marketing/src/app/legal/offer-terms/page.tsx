import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = {title:"Megas WoW India pricing and offer terms | MegasMoves",description:"Current India test pricing and disclosures required before a live subscription offer."};
const sections = [
  {
    "title": "Current India test pricing",
    "paragraphs": [
      "The Razorpay test checkout displays ₹999 per month for 12 monthly billing cycles. It is an integration test and makes no real charge. This test configuration is not an approved live price or promotional offer."
    ]
  },
  {
    "title": "Live offers and renewals",
    "paragraphs": [
      "A live promotional offer has not been published here. Before a real purchase, checkout must disclose the price, currency, applicable taxes, billing interval, any discounted cycles and the amount and timing of renewals. The 12-cycle test configuration does not authorize a later live charge or an undisclosed price increase."
    ]
  },
  {
    "title": "Payment providers",
    "paragraphs": [
      "Razorpay Test Mode checkout is currently available. Dodo Payments test billing setup is in progress and its checkout is not yet available on this site. Test transactions do not move real money or authorize future live charges."
    ]
  }
];
export default function PolicyPage(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-4xl px-6 py-16 md:py-24">
 <p className="eyebrow">AMRETRA DYNAMICS / INDIA / POLICY</p>
 <h1 className="mt-5 text-4xl font-extrabold tracking-[-0.05em] md:text-6xl">Megas WoW India pricing and offer terms</h1>
 <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">Current India test pricing and disclosures required before a live subscription offer.</p>
 <p className="mt-4 text-sm text-[var(--muted)]">Updated 28 November 2026</p>
 <div className="mt-14 space-y-9">{sections.map((section,index)=><section key={section.title} className="border-t border-[var(--line)] pt-7">
  <p className="text-xs font-extrabold text-[var(--accent)]">{String(index+1).padStart(2,"0")}</p>
  <h2 className="mt-3 text-2xl font-bold">{section.title}</h2>
  {section.paragraphs.map((text)=><p key={text} className="mt-4 text-base leading-8 text-[var(--text-soft)]">{text}</p>)}
 </section>)}</div>
 <nav aria-label="Related policies" className="mt-12 flex flex-wrap gap-4 border-t border-[var(--line)] pt-7 text-sm">
  <Link href="/legal/subscription-terms/">Subscription terms</Link><Link href="/legal/offer-terms/">India offer</Link>
  <Link href="/legal/cancellation/">Cancellation</Link><Link href="/legal/refunds/">Refunds</Link>
  <Link href="/legal/delivery/">Service delivery</Link><Link href="/legal/service-privacy/">Service privacy</Link><Link href="/legal/">Legal center</Link>
 </nav><p className="mt-8 text-sm text-[var(--muted)]">Billing support: <a href="mailto:master@megasmoves.io">master@megasmoves.io</a></p>
 </main><Footer/></div>}
