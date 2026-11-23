import Header from "@/components/Header";
import Footer from "@/components/Footer";
import Link from "next/link";

export const metadata = { title: "Android installation and updates | Megas Moves" };

export default function Page() {
  return <div className="site-surface"><Header/><main className="manual-page marketing-manual">
    <p className="eyebrow">Android distribution</p><h1>Install and update Megas Moves on Android.</h1>
    <p>Choose the channel you already use. Google Play and direct APK downloads have different signing and update processes.</p>
    <h2>Google Play</h2>
    <p>Google Play manages updates for Play-installed copies. Public access to the listing has not been verified; an internal or closed-test listing can be unavailable to other accounts.</p>
    <a href="https://play.google.com/store/apps/details?id=com.amretra.megasmoves" className="button button--quiet">Check Google Play listing</a>
    <h2>Direct APK</h2>
    <p>Use the versioned Android APK in the <Link href="/download">download catalog</Link>. Review its SHA-256, application ID, version and signing identity before installing it. Newer lab and debug builds may not be compatible with your installed signature.</p>
    <Link href="/download" className="button button--primary">View direct APK download</Link>
    <p>The <a href="/mobile/releases/latest.json">direct-install release feed</a> is separate from Google Play and intentionally does not list unverified lab builds.</p>
    <h2>Protect your existing data</h2>
    <p>An in-place APK update requires the same application ID and signing certificate, plus a higher version code. If Android reports a mismatch, stop. Do not uninstall to force an upgrade; use the original signing channel or request a verified transition build.</p>
    <h2>Testing and feedback</h2>
    <p>Installing alone does not prove secure pairing or device control. Include app and Android versions with redacted reproduction steps. Never share credentials.</p>
    <a href="https://github.com/Amretra-Dynamics/megasmoves/issues" className="button button--quiet">Report an issue</a>
  </main><Footer/></div>;
}
