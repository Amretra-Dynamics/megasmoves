import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = {title:"Megas WoW subscription terms | MegasMoves",description:"Terms for Megas WoW customers in India, separate from the public marketing website."};
const sections = [
  {
    "title": "Scope and availability",
    "paragraphs": [
      "Megas WoW is an account-based SI-agent service intended for India. Only capabilities in the customer's purchase summary are included; preview features are not guaranteed. The current checkout operates in Razorpay Test Mode and collects no real funds."
    ]
  },
  {
    "title": "Pricing and promotional offer",
    "paragraphs": [
      "Regular price: ₹1,999 per month. A limited-time price of ₹999 per month is advertised for eligible India customers. The current Razorpay monthly subscription authorization covers 12 billing cycles; further payments require the appropriate authorization. International checkout is not offered. The promotion period, number of discounted cycles, taxes and renewal price must be disclosed before authorization. The renewal amount cannot be silently changed."
    ]
  },
  {
    "title": "Payment and activation",
    "paragraphs": [
      "Paid access requires a verified payment matched to the authenticated account. Recurring debits require the customer's authorization and applicable RBI, bank and provider mandate processes. Browser redirects and unsigned notifications do not establish payment."
    ]
  },
  {
    "title": "Authorized use and changes",
    "paragraphs": [
      "Use only devices, accounts and external services you are authorized to control. Protect credentials and access grants. Material changes to paid scope or charges require appropriate notice and authorization. Statutory consumer rights remain applicable."
    ]
  },
  {
    "title": "Cancellation, refunds and privacy",
    "paragraphs": [
      "Refer to the Cancellation Policy, Refund Policy and Service Privacy Notice. For account and billing assistance contact master@megasmoves.io from the account email, without disclosing passwords, tokens or full card numbers."
    ]
  }
];
export default function PolicyPage(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-4xl px-6 py-16 md:py-24">
 <p className="eyebrow">AMRETRA DYNAMICS / INDIA / POLICY</p>
 <h1 className="mt-5 text-4xl font-extrabold tracking-[-0.05em] md:text-6xl">Megas WoW subscription terms</h1>
 <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">Terms for Megas WoW customers in India, separate from the public marketing website.</p>
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
