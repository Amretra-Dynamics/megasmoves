import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = {title:"Megas WoW service privacy notice | MegasMoves",description:"Privacy information for the managed Megas WoW account, payment and connected-device service, separate from the static website."};
const sections = [
  {
    "title": "Account and payments",
    "paragraphs": [
      "Firebase Authentication verifies accounts and may provide an account identifier and profile email. Razorpay handles payment details; MegasMoves does not ask for full card credentials on its own checkout. We may process provider subscription references, billing state and webhook verification for customer entitlements."
    ]
  },
  {
    "title": "Devices and platform",
    "paragraphs": [
      "Cloudflare Workers and D1 support account-scoped device information, access grants, subscription state and security audit data needed for authorized services. Access to devices must be consented to by an authorized owner. Third-party providers have independent privacy terms."
    ]
  },
  {
    "title": "Security, retention and customer rights",
    "paragraphs": [
      "We use authentication, scoped permissions and transaction verification. Records may be retained as needed for service delivery, security, dispute resolution, accounting and legal obligations. Specific retention schedules must be finalized before live payments. Request access, correction or an appropriate deletion review through master@megasmoves.io."
    ]
  },
  {
    "title": "Test environment",
    "paragraphs": [
      "The current checkout is a test integration. Test transaction artifacts are not real-money purchases. This service notice supplements the separate marketing website privacy notice."
    ]
  }
];
export default function PolicyPage(){return <div className="site-surface min-h-screen"><Header/><main className="mx-auto max-w-4xl px-6 py-16 md:py-24">
 <p className="eyebrow">AMRETRA DYNAMICS / INDIA / POLICY</p>
 <h1 className="mt-5 text-4xl font-extrabold tracking-[-0.05em] md:text-6xl">Megas WoW service privacy notice</h1>
 <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">Privacy information for the managed Megas WoW account, payment and connected-device service, separate from the static website.</p>
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
