"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Mail, MessageSquare, Users } from "lucide-react";
import Header from "@/components/Header";
import Footer from "@/components/Footer";

interface TestingConfig {
  whatsapp_link: string;
}

export default function TestingFeedbackPage() {
  const [config, setConfig] = useState<TestingConfig>({
    whatsapp_link: "https://chat.whatsapp.com/Ivc6BIPYxEVKIWhAYCPVvd",
  });

  useEffect(() => {
    const controller = new AbortController();

    async function loadTestingConfig() {
      try {
        const response = await fetch("/api/v1/testing/config", {
          signal: controller.signal,
        });
        const contentType = response.headers.get("content-type") ?? "";
        if (!response.ok || !contentType.includes("application/json")) return;

        const data = (await response.json()) as Partial<TestingConfig>;
        if (data.whatsapp_link) setConfig({ whatsapp_link: data.whatsapp_link });
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
      }
    }

    void loadTestingConfig();
    return () => controller.abort();
  }, []);

  const mailtoLink =
    "mailto:root@megasmoves.io?subject=Megas%20Moves%20Testing%20Feedback&body=Hey%20Megas%20Team,%20here%20is%20my%20feedback%20on%20the%20testing%20app:";

  return (
    <div className="site-surface flex min-h-screen flex-1 flex-col overflow-x-hidden font-sans text-deep-ink selection:bg-brand-orange selection:text-white">
      <Header />

      <main className="flex-1">
        <section className="relative overflow-hidden border-b border-[#172033]/10 bg-[#c9dde9] px-6 py-16 md:py-24">
          <div className="hero-ghost-word mega-word pointer-events-none absolute left-1/2 top-5 -translate-x-1/2 text-[16vw] font-bold leading-none text-[#fffdf8]/58">
            MEGAS
          </div>
          <div className="relative z-10 mx-auto max-w-5xl">
            <Link
              href="/android-testing"
              className="inline-flex items-center gap-2 text-sm font-semibold text-[#234157] transition-colors hover:text-[#00a8b0]"
            >
              <ArrowLeft className="h-4 w-4" />
              Back to testing onboarding
            </Link>
            <p className="mt-10 text-sm font-semibold text-[#00a8b0]">Testing feedback loop</p>
            <h1 className="mt-4 max-w-3xl text-5xl font-semibold leading-[0.98] tracking-[-0.04em] text-[#172033] md:text-7xl">
              Send sharp feedback to the build team.
            </h1>
            <p className="mt-6 max-w-2xl text-base font-medium leading-8 text-[#234157]">
              Report install issues, broken flows, confusing copy, crashes, or anything that blocks the mobile context workflow.
            </p>
          </div>
        </section>

        <section className="px-6 py-16">
          <div className="mx-auto grid max-w-5xl gap-5 md:grid-cols-2">
            <a
              href={config.whatsapp_link}
              target="_blank"
              rel="noopener noreferrer"
              className="group rounded-[1.5rem] border border-[#172033]/12 bg-[#fffdf8] p-7 transition-transform hover:-translate-y-0.5"
            >
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-[#172033]/10 bg-[#e6fbf9] text-[#00a8b0]">
                <Users className="h-5 w-5" />
              </div>
              <h2 className="mt-6 text-2xl font-semibold text-deep-ink">WhatsApp updates</h2>
              <p className="mt-3 text-sm leading-7 text-[#53627a]">
                Fastest route for screenshots, install blockers, Play Store access questions, and tester coordination.
              </p>
              <span className="mt-6 inline-flex items-center gap-2 text-sm font-semibold text-brand-orange-deep group-hover:underline">
                Join the group
                <MessageSquare className="h-4 w-4" />
              </span>
            </a>

            <a
              href={mailtoLink}
              className="group rounded-[1.5rem] border border-[#172033]/12 bg-white/70 p-7 transition-transform hover:-translate-y-0.5"
            >
              <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-[#172033]/10 bg-[#fffdf8] text-[#00a8b0]">
                <Mail className="h-5 w-5" />
              </div>
              <h2 className="mt-6 text-2xl font-semibold text-deep-ink">Email feedback</h2>
              <p className="mt-3 text-sm leading-7 text-[#53627a]">
                Best for detailed bug reports, device details, logs, reproduction steps, and product suggestions.
              </p>
              <span className="mt-6 inline-flex items-center gap-2 text-sm font-semibold text-brand-orange-deep group-hover:underline">
                Email root@megasmoves.io
                <Mail className="h-4 w-4" />
              </span>
            </a>
          </div>
        </section>
      </main>

      <Footer />
    </div>
  );
}
