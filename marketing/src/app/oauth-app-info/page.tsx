import Link from "next/link";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata={title:"ChatGPT plugin - Coming soon | Megas Moves"};
export default function Page(){return <div className="site-surface"><Header/><main className="manual-page marketing-manual"><p className="eyebrow">ChatGPT integration</p><h1>ChatGPT plugin - coming soon.</h1><p>Private integration testing is in progress. This page does not advertise a public install or connect your computer.</p><h2>One local operator</h2><p>Megas MCP lives inside MegasMoves. Remote clients authenticate and route to the account-owned live operator; the marketing website does not host another capability core.</p><Link href="/docs/cli-reference" className="button button--primary">Integration reference</Link></main><Footer/></div>;}
