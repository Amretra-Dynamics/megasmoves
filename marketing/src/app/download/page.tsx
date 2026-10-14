import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";
import { ArrowRight, Laptop2, LockKeyhole, ShieldCheck, Zap } from "lucide-react";
import Footer from "@/components/Footer";
import Header from "@/components/Header";
import WindowsInstaller from "@/components/download/WindowsInstaller";
import TerminalInstall from "@/components/download/TerminalInstall";
import HashNavigation from "@/components/HashNavigation";
export const metadata: Metadata = { title: "Download Megas Moves", description: "Megas Moves preview downloads for Windows, macOS, Linux and Android, with versions, file sizes and SHA-256 checksums." };
const steps = [
  { title: "Install", copy: "Use the Windows installer to update the existing MegasMoves installation in place.", icon: Laptop2 },
  { title: "Sign in", copy: "Sign in inside the app to connect your account.", icon: LockKeyhole },
  { title: "Choose your work", copy: "Add folders and files to a Megas Action, then create the tasks you want to work on.", icon: ShieldCheck },
];
export default function DownloadPage() {
  return <div className="download-page"><Header /><HashNavigation /><main>
    <section className="download-hero" aria-labelledby="download-title"><div className="download-hero__inner">
      <div className="download-hero__copy"><p className="eyebrow"><Zap aria-hidden="true" />Megas Moves downloads</p>
        <h1 id="download-title" className="font-display-hero">Get Megas.<strong> Make moves.</strong></h1>
        <p>Choose a Windows, macOS, Linux or Android download. Check the version, file size and checksum before installing.</p>
        <div className="download-hero__actions"><a href="#downloads" className="button button--primary button--large">See available builds<ArrowRight aria-hidden="true" /></a><Link href="/docs/getting-started" className="button button--quiet button--large">Setup guide<ArrowRight aria-hidden="true" /></Link></div>
      </div><div className="download-hero__visual" aria-hidden="true"><Image src="/megas-logo-4k.png" alt="" fill priority sizes="(max-width: 900px) 100vw, 48vw" /></div>
    </div></section>
    <section id="downloads" className="download-catalog" aria-labelledby="downloads-title"><div id="platforms" className="section-heading"><p className="eyebrow">Choose your platform</p><h2 id="downloads-title">Megas Moves.<br />On your device.</h2><Link href="/docs/getting-started" className="text-link">Installation guide<ArrowRight aria-hidden="true" /></Link></div><WindowsInstaller /><TerminalInstall /></section>
    <section className="download-steps" aria-label="More ways to connect"><article id="chatgpt"><h2>ChatGPT plugin</h2><p>Coming soon. Bring ChatGPT to your local Megas Moves operator.</p></article><article><h2>Open source</h2><p>Explore the code, documentation and issues on GitHub.</p><a href="https://github.com/Amretra-Dynamics/megasmoves" className="text-link">View source<ArrowRight aria-hidden="true" /></a></article><article><h2>Need a hand?</h2><p>Find installation steps and account setup in the documentation.</p><Link href="/docs/getting-started" className="text-link">Read the guide<ArrowRight aria-hidden="true" /></Link></article></section>
    <section className="download-steps" aria-label="Setup steps">{steps.map((step, index) => { const Icon = step.icon; return <article key={step.title}><div className="download-step__topline"><span>0{index + 1}</span><Icon aria-hidden="true" /></div><h2>{step.title}</h2><p>{step.copy}</p></article>; })}</section>
  </main><Footer /></div>;
}
