import type { Metadata } from "next";
import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";

export const metadata: Metadata = {
  title: "Founder, Leadership & Team | MegasMoves",
  description:
    "Public founder and leadership information for Amretra Dynamics, the Hyderabad-based company developing MegasMoves.",
  alternates: { canonical: "/team/" },
};

const organization = {
  "@context": "https://schema.org",
  "@type": "Organization",
  name: "Amretra Dynamics",
  url: "https://amretra.com/",
  founder: {
    "@type": "Person",
    name: "Musrif Abhinav",
    jobTitle: "Founder and Software Engineer",
    url: "https://abhinavmusrif.me/",
    sameAs: [
      "https://www.linkedin.com/in/musrifabhinav",
      "https://github.com/abhinavmusrif",
    ],
  },
};

export default function TeamPage() {
  return (
    <div className="site-surface min-h-screen">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(organization) }}
      />
      <Header />
      <main className="mx-auto max-w-6xl px-6 py-16 md:py-24">
        <p className="eyebrow">AMRETRA DYNAMICS / PEOPLE</p>
        <h1 className="mt-5 max-w-5xl text-5xl font-extrabold tracking-[-0.06em] md:text-7xl">
          Founder, leadership <span className="text-[var(--accent)]">and team.</span>
        </h1>
        <p className="mt-6 max-w-3xl text-lg leading-8 text-[var(--text-soft)]">
          MegasMoves is developed by Amretra Dynamics, a founder-led software
          product company based in Hyderabad, India. This page identifies the
          people publicly responsible for the company and product.
        </p>

        <section
          aria-labelledby="founder-name"
          className="mt-14 rounded-xl border border-[var(--line-strong)] bg-[var(--surface-1)] p-7 md:p-10"
        >
          <p className="eyebrow">Founder and public leadership</p>
          <h2 id="founder-name" className="mt-4 text-3xl font-extrabold tracking-tight">
            Musrif Abhinav
          </h2>
          <p className="mt-2 font-semibold text-[var(--accent)]">
            Founder and Software Engineer, Amretra Dynamics
          </p>
          <p className="mt-6 max-w-3xl leading-8 text-[var(--text-soft)]">
            Musrif Abhinav founded Amretra Dynamics and works on backend
            systems, developer tools and connected-device engineering. His
            publicly documented work includes the MegasMoves SI-agent platform,
            Python and TypeScript services, Linux systems, and Android integrations.
            He leads the company&apos;s founder-led product engineering.
          </p>
          <div className="mt-8 flex flex-wrap gap-x-7 gap-y-4 text-sm font-semibold">
            <a href="https://abhinavmusrif.me/" className="text-[var(--accent)] underline underline-offset-4">
              Founder&apos;s engineering portfolio
            </a>
            <a href="https://www.linkedin.com/in/musrifabhinav" className="text-[var(--accent)] underline underline-offset-4">
              Founder&apos;s LinkedIn profile
            </a>
            <a href="https://github.com/abhinavmusrif" className="text-[var(--accent)] underline underline-offset-4">
              Founder&apos;s GitHub
            </a>
          </div>
        </section>

        <section aria-labelledby="staff-heading" className="mt-12 max-w-3xl">
          <h2 id="staff-heading" className="text-2xl font-bold">Leadership and key staff</h2>
          <p className="mt-4 leading-8 text-[var(--text-soft)]">
            Amretra Dynamics is currently presented publicly as a founder-led
            company. Musrif Abhinav is its named founder and publicly identified
            engineering lead. No additional leadership or key staff are publicly
            announced here. This page will be updated if new appointments are
            publicly confirmed.
          </p>
        </section>

        <section aria-labelledby="about-heading" className="mt-12 max-w-3xl border-t border-[var(--line)] pt-10">
          <h2 id="about-heading" className="text-2xl font-bold">Company and product</h2>
          <p className="mt-4 leading-8 text-[var(--text-soft)]">
            Amretra Dynamics develops software tools for devices and developer
            workflows. MegasMoves is its open-source SI-agent product with
            permission-aware local tools, task context and native-device
            integrations. Product features and release availability are
            described separately and are not represented as a finished
            commercial release here.
          </p>
          <div className="mt-6 flex flex-wrap gap-x-7 gap-y-4 text-sm font-semibold">
            <a href="https://amretra.com/" className="text-[var(--accent)] underline underline-offset-4">
              Amretra Dynamics website
            </a>
            <a href="https://github.com/Amretra-Dynamics/megasmoves" className="text-[var(--accent)] underline underline-offset-4">
              MegasMoves public source
            </a>
            <Link href="/download/" className="text-[var(--accent)] underline underline-offset-4">
              Verified release information
            </Link>
          </div>
        </section>

        <section aria-labelledby="contact-heading" className="mt-12 max-w-3xl border-t border-[var(--line)] pt-10">
          <h2 id="contact-heading" className="text-2xl font-bold">Business enquiries</h2>
          <p className="mt-4 leading-8 text-[var(--text-soft)]">
            For product or company enquiries concerning MegasMoves, contact{" "}
            <a href="mailto:master@megasmoves.io" className="font-semibold text-[var(--accent)] underline underline-offset-4">
              master@megasmoves.io
            </a>.
          </p>
        </section>
      </main>
      <Footer />
    </div>
  );
}
