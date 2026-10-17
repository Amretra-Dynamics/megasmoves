import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = {title:"Megas WoW cancellation policy | MegasMoves",description:"How India customers can stop recurring subscription renewals."};
const sections = [
  {
    "title": "Cancellation requests",
    "paragraphs": [
      "Email master@megasmoves.io from the account email with the subject Cancel Megas WoW and the subscription reference. Never send a password or full card number. Until a verified self-service control is available, support will verify ownership and confirm the cancellation outcome."
    ]
  },
  {
    "title": "Recurring mandates",
    "paragraphs": [
      "When cancellation is processed and confirmed, MegasMoves must not initiate further renewals. Customers can separately inspect or revoke payment mandates through their bank, UPI app or Razorpay when supported. Confirmation should state any remaining access through the already-paid period."
    ]
  },
  {
    "title": "Billing issues",
    "paragraphs": [
      "Report charges after confirmed cancellation with the transaction reference. Cancellation of future renewals and refunds of earlier charges are separate processes. Test Mode never moves real money."
    ]
  }
];
export default function PolicyPage(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-4xl px-6 py-16 md:py-24">
 <p className="eyebrow">AMRETRA DYNAMICS / INDIA / POLICY</p>
 <h1 className="mt-5 text-4xl font-extrabold tracking-[-0.05em] md:text-6xl">Megas WoW cancellation policy</h1>
 <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">How India customers can stop recurring subscription renewals.</p>
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
