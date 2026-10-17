import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = {title:"Megas WoW India promotional offer | MegasMoves",description:"Limited-time promotional subscription pricing for customers in India."};
const sections = [
  {
    "title": "India-only prices",
    "paragraphs": [
      "The standard monthly price is ₹1,999. A limited-time offer advertises ₹999 per month for eligible India customers. The current subscription authorization covers 12 monthly billing cycles at ₹999; it does not authorize an unannounced higher price afterward. No international purchase is currently offered. All amounts and applicable taxes must be clear before authorization."
    ]
  },
  {
    "title": "Offer period and renewals",
    "paragraphs": [
      "The final expiry date and number of discounted billing cycles have not been announced. Before any live purchase, checkout must disclose which cycles qualify, the renewal amount and schedule, and any change to the promotional price. An undisclosed automatic increase is not authorized."
    ]
  },
  {
    "title": "Test checkout",
    "paragraphs": [
      "Razorpay Test Mode is currently used for integration testing only. No real payment is made, and test subscriptions are not active paid subscriptions. The offer may be withdrawn prospectively for new purchases, subject to existing customer commitments and law."
    ]
  }
];
export default function PolicyPage(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-4xl px-6 py-16 md:py-24">
 <p className="eyebrow">AMRETRA DYNAMICS / INDIA / POLICY</p>
 <h1 className="mt-5 text-4xl font-extrabold tracking-[-0.05em] md:text-6xl">Megas WoW India promotional offer</h1>
 <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">Limited-time promotional subscription pricing for customers in India.</p>
 <p className="mt-4 text-sm text-[var(--muted)]">Updated 17 October 2026</p>
 <div className="mt-14 space-y-9">{sections.map((section,index)=><section key={section.title} className="border-t border-[var(--line)] pt-7">
  <p className="text-xs font-extrabold text-[var(--accent)]">{String(index+1).padStart(2,"0")}</p>
  <h2 className="mt-3 text-2xl font-bold">{section.title}</h2>
  {section.paragraphs.map((text)=><p key={text} className="mt-4 text-base leading-8 text-[var(--text-soft)]">{text}</p>)}
 </section>)}</div>
 <nav aria-label="Related policies" className="mt-12 flex flex-wrap gap-4 border-t border-[var(--line)] pt-7 text-sm">
  <Link href="/legal/subscription-terms/">Subscription terms</Link><Link href="/legal/offer-terms/">India offer</Link>
  <Link href="/legal/cancellation/">Cancellation</Link><Link href="/legal/refunds/">Refunds</Link>
  <Link href="/legal/service-privacy/">Service privacy</Link><Link href="/legal/">Legal center</Link>
 </nav><p className="mt-8 text-sm text-[var(--muted)]">Billing support: <a href="mailto:master@megasmoves.io">master@megasmoves.io</a></p>
 </main><Footer/></div>}
