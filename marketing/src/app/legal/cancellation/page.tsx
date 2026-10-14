import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export default function Page(){return <div className="site-surface"><Header/><main className="manual-page marketing-manual"><p className="eyebrow">Megas Moves website</p><h1>Cancellation</h1><p>This website provides product information and preview downloads. It does not sell subscriptions or take payments. Megas WoW is in development.</p><p>Read the website terms and privacy notice for the current site. Software use is governed by its included license. Only operate devices and accounts you are authorized to use.</p><nav><Link href="/terms">Website terms</Link><Link href="/privacy">Privacy notice</Link><Link href="/legal">Legal center</Link></nav></main><Footer/></div>;}
