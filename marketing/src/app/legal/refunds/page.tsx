import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = {title:"Megas WoW refund policy | MegasMoves",description:"Payment corrections and refund requests for Megas WoW customers in India."};
const sections = [
  {
    "title": "Request a refund review",
    "paragraphs": [
      "Email master@megasmoves.io from the linked account with the charge date, amount, payment reference and reason. Requests for duplicate, mistaken or unauthorized charges are investigated against provider records and applicable Indian consumer law. Do not send sensitive payment credentials."
    ]
  },
  {
    "title": "Refund processing",
    "paragraphs": [
      "Approved refunds are ordinarily returned through the original payment method where supported. Processing time depends on the payment processor, bank or network. We will provide a decision and available transaction references. This policy does not waive mandatory consumer remedies."
    ]
  },
  {
    "title": "Refunds and cancellation",
    "paragraphs": [
      "Cancelling future renewal does not itself reverse a past payment. For future charges, see the Cancellation Policy. The current Razorpay Test Mode checkout has not collected actual money."
    ]
  }
];
export default function PolicyPage(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-4xl px-6 py-16 md:py-24">
 <p className="eyebrow">AMRETRA DYNAMICS / INDIA / POLICY</p>
 <h1 className="mt-5 text-4xl font-extrabold tracking-[-0.05em] md:text-6xl">Megas WoW refund policy</h1>
 <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">Payment corrections and refund requests for Megas WoW customers in India.</p>
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
