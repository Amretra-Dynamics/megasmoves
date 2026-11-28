import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = {title:"Megas WoW cancellation policy | MegasMoves",description:"How India customers can stop recurring subscription renewals."};
const sections = [
  {
    "title": "Cancellation requests",
    "paragraphs": [
      "Use the cancellation control for your subscription in the Megas billing account or the selected provider’s customer portal when available. If a control is unavailable or cancellation is not confirmed, email master@megasmoves.io from the account email with the subject Cancel Megas WoW, the provider name and subscription reference. Support will verify ownership and confirm the outcome. Never send a password or full card number."
    ]
  },
  {
    "title": "Recurring mandates",
    "paragraphs": [
      "After cancellation is confirmed and effective, no further subscription renewals will be initiated. An already-paid period may continue as stated in the confirmation. Check the confirmation for the effective date and remaining access. Razorpay subscriptions and Dodo merchant-of-record subscriptions use the respective provider’s supported cancellation process. Where supported, customers can also inspect or revoke mandates through their bank or UPI app."
    ]
  },
  {
    "title": "Billing issues",
    "paragraphs": [
      "Report charges after confirmed cancellation with the provider and transaction reference. Cancellation of future renewals and refunds of earlier charges are separate processes. The current Razorpay checkout is in Test Mode; Dodo test billing setup is in progress. Test Mode does not move real money."
    ]
  }
];
export default function PolicyPage(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-4xl px-6 py-16 md:py-24">
 <p className="eyebrow">AMRETRA DYNAMICS / INDIA / POLICY</p>
 <h1 className="mt-5 text-4xl font-extrabold tracking-[-0.05em] md:text-6xl">Megas WoW cancellation policy</h1>
 <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">How India customers can stop recurring subscription renewals.</p>
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
