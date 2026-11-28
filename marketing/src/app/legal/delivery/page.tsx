import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";

export const metadata = {
  title: "Service delivery and access | MegasMoves",
  description: "How Megas Moves preview downloads and Megas WoW test access are provided, with help for download, account and billing issues.",
  alternates: { canonical: "/legal/delivery/" },
};

const sections = [
  {
    title: "Digital software delivery",
    paragraphs: [
      "Megas Moves is delivered digitally. No physical goods are shipped. Preview installers, packages and installation instructions are provided on the Downloads page. Check the release status and requirements for your platform before installing; available functions vary by platform and release.",
      "Downloading or installing a preview does not purchase a subscription or grant access to a device. Software use remains subject to its included licence, and device and account access require the authorised owner's permission.",
    ],
  },
  {
    title: "Your account and test access",
    paragraphs: [
      "Sign in with a verified Megas account before using test checkout. Use that same account to view your plan and billing status. Subscription access depends on verified provider records matched to your account; a checkout return page or payment authorisation alone does not establish paid access.",
      "The current checkout is for testing. It does not purchase a live service or authorise a future real-money charge. Test account status and access checks may change as the integration is verified. An immediate activation time is not promised for this preview.",
    ],
  },
  {
    title: "Access periods and cancellation",
    paragraphs: [
      "Where a verified access period is shown, access depends on that period and the current subscription status. Requesting cancellation and confirming its effective date are separate steps. Check the provider's confirmation for when renewals stop and any remaining access.",
      "Cancellation, refunds and account deletion are separate processes. Cancelling a test subscription does not delete your Megas account or its billing history. The Cancellation Policy, Refund Policy and Service Privacy Notice explain the current support routes and data-handling boundaries.",
    ],
  },
  {
    title: "Help with delivery or access",
    paragraphs: [
      "If a download is unavailable, sign-in fails or your account status does not match the expected test result, contact master@megasmoves.io. Include the platform, app version, a brief description and any relevant test subscription or payment reference. For account issues, write from the linked account email.",
      "Do not send passwords, access tokens, secret keys or full card numbers. Support can review the reported issue and available account or provider records. A fixed resolution or refund-processing time is not promised by this preview policy.",
    ],
  },
  {
    title: "Before a live offer",
    paragraphs: [
      "Real-money subscriptions are not available on this site. Before any live offer, its terms must state the included service, price and taxes, renewal schedule, when access is provided and how failed delivery is handled. Live payment and access delivery must be verified, and the service retention schedule must be finalised.",
      "The current India test offering does not establish international payment availability. Refer to the pricing and provider information shown at the time; the test configuration is not a live offer.",
    ],
  },
];

export default function DeliveryPolicy() {
  return (
    <div className="site-surface min-h-screen">
      <Header />
      <main className="mx-auto max-w-4xl px-6 py-16 md:py-24">
        <p className="eyebrow">AMRETRA DYNAMICS / INDIA / POLICY</p>
        <h1 className="mt-5 text-4xl font-extrabold tracking-[-0.05em] md:text-6xl">Service delivery and access</h1>
        <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">How preview downloads and Megas WoW test access are provided, and where to get help.</p>
        <p className="mt-4 text-sm text-[var(--muted)]">Updated 28 November 2026</p>

        <aside aria-label="Current test offering" className="mt-9 rounded-xl border border-[var(--line-strong)] bg-[var(--surface-2)] p-6 md:p-7">
          <p className="text-sm font-bold text-[var(--accent)]">Razorpay TEST · ₹999/month · 12 test cycles</p>
          <p className="mt-3 leading-7 text-[var(--text-soft)]">No money is collected. Dodo Payments test billing setup is in progress; its checkout is not available on this site yet.</p>
          <div className="mt-5 flex flex-wrap gap-x-6 gap-y-3 text-sm font-semibold">
            <Link href="/download/">Preview downloads</Link>
            <a href="https://pay.megasmoves.io/account">Open your account</a>
            <Link href="/pricing/">Test pricing</Link>
          </div>
        </aside>

        <div className="mt-12 space-y-9">
          {sections.map((section, index) => (
            <section key={section.title} className="border-t border-[var(--line)] pt-7">
              <p className="text-xs font-extrabold text-[var(--accent)]">{String(index + 1).padStart(2, "0")}</p>
              <h2 className="mt-3 text-2xl font-bold">{section.title}</h2>
              {section.paragraphs.map((paragraph) => (
                <p key={paragraph} className="mt-4 text-base leading-8 text-[var(--text-soft)]">{paragraph}</p>
              ))}
            </section>
          ))}
        </div>

        <nav aria-label="Related policies" className="mt-12 flex flex-wrap gap-4 border-t border-[var(--line)] pt-7 text-sm">
          <Link href="/legal/subscription-terms/">Subscription terms</Link>
          <Link href="/legal/offer-terms/">India offer</Link>
          <Link href="/legal/cancellation/">Cancellation</Link>
          <Link href="/legal/refunds/">Refunds</Link>
          <Link href="/legal/service-privacy/">Service privacy</Link>
          <Link href="/legal/">Legal center</Link>
        </nav>
        <p className="mt-8 text-sm text-[var(--muted)]">Delivery and billing support: <a href="mailto:master@megasmoves.io">master@megasmoves.io</a></p>
      </main>
      <Footer />
    </div>
  );
}
