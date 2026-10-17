import Image from "next/image";
import Link from "next/link";
import { ArrowRight, Radio, Layers3, History, ShieldCheck, Zap, CheckCircle2 } from "lucide-react";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata = { title: "Megas WoW — Your always-on SI agent" };
const capabilities = [
  { icon: Radio, title: "Connected devices", text: "Bring your devices and compatible agents into the same workflow." },
  { icon: Layers3, title: "Agent coordination", text: "Organize ongoing work through Megas Actions and tasks." },
  { icon: History, title: "Work continuity", text: "Keep context and evidence available across interrupted sessions." },
  { icon: ShieldCheck, title: "Visible control", text: "Know what is working, what needs you and what happened." },
];
export default function WoWPage() {
  return <div className="landing-page"><Header /><main>
    <section className="velocity-hero" aria-labelledby="wow-title">
      <div className="velocity-hero__inner">
        <div className="velocity-hero__copy">
          <p className="eyebrow hero-eyebrow"><Zap aria-hidden="true" />Megas WoW · Early access</p>
          <h1 id="wow-title" className="velocity-hero__title"><span className="velocity-hero__promise">Your always-on<br /><strong>SI agent.</strong></span></h1>
          <p className="velocity-hero__lede">For all your devices. Megas WoW brings agents, context and ongoing work together through Megas Moves.</p>
          <div className="velocity-hero__actions"><a className="button button--primary button--large" href="https://pay.megasmoves.io/">View India checkout (Test Mode)<ArrowRight aria-hidden="true" /></a><Link className="button button--quiet button--large" href="#wow-plan">Explore Megas WoW<ArrowRight aria-hidden="true" /></Link></div>
        </div>
        <div className="velocity-hero__visual" aria-hidden="true"><span className="speed-line speed-line--one" /><span className="speed-line speed-line--two" /><span className="speed-line speed-line--three" /><span className="speed-node speed-node--one" /><span className="speed-node speed-node--two" /><span className="speed-node speed-node--three" /><div className="velocity-hero__mark"><Image src="/megas-logo-4k.png" alt="" fill priority sizes="(max-width: 900px) 92vw, 54vw" /></div></div>
      </div>
      <div className="feature-rail" aria-label="Planned Megas WoW capabilities">{capabilities.map(({icon:Icon,title,text})=><article key={title}><Icon aria-hidden="true" /><div><h2>{title}</h2><p>{text}</p></div></article>)}</div>
    </section>
    <section id="wow-plan" className="workflow-band reveal-section" aria-labelledby="wow-plan-title">
      <div className="workflow-band__intro"><p className="eyebrow">One intended subscription</p><h2 id="wow-plan-title">Megas WoW.<br /><strong>₹1,999/month regular.</strong><br /><span className="text-[var(--accent)]">Limited-time India offer: ₹999/month.</span></h2></div>
      <ol className="workflow-steps"><li><div className="workflow-step__topline"><span>01</span><h3>Always-on work</h3></div><div className="workflow-step__body"><span aria-hidden="true"><Radio /></span><p>Designed for connected agents and work that continues across your devices.</p></div></li><li><div className="workflow-step__topline"><span>02</span><h3>Shared context</h3></div><div className="workflow-step__body"><span aria-hidden="true"><History /></span><p>Keep Actions, tasks and evidence together so agents can pick up ongoing work.</p></div></li><li><div className="workflow-step__topline"><span>03</span><h3>Early access</h3></div><div className="workflow-step__body"><span aria-hidden="true"><CheckCircle2 /></span><p>India-only checkout is currently available for Razorpay Test Mode integration. Real paid subscriptions are not activated yet. Review offer, renewal and cancellation terms before a live purchase.</p></div></li></ol>
    </section>
    <section className="plans-band reveal-section" aria-labelledby="wow-start-title"><div className="section-heading plans-band__heading"><p className="eyebrow">Start with Megas Moves</p><h2 id="wow-start-title">Your devices. Your tools. Your next move.</h2><Link className="text-link" href="/download">Get Megas Moves<ArrowRight aria-hidden="true" /></Link></div></section>
  </main><Footer /></div>;
}
