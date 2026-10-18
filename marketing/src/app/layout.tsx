import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

const display = localFont({variable: "--font-display", display: "swap", src: [{path: "./fonts/barlow-condensed-600.ttf", weight: "600", style: "normal"}, {path: "./fonts/barlow-condensed-700.ttf", weight: "700", style: "normal"}, {path: "./fonts/barlow-condensed-800.ttf", weight: "800", style: "normal"}, {path: "./fonts/barlow-condensed-900.ttf", weight: "900", style: "normal"}]});
const inter = localFont({variable: "--font-inter", display: "swap", src: [{path: "./fonts/inter-400.ttf", weight: "400", style: "normal"}, {path: "./fonts/inter-600.ttf", weight: "600", style: "normal"}, {path: "./fonts/inter-700.ttf", weight: "700", style: "normal"}, {path: "./fonts/inter-800.ttf", weight: "800", style: "normal"}, {path: "./fonts/inter-900.ttf", weight: "900", style: "normal"}]});

const siteUrl = "https://megasmoves.io";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: "MegasMoves | SI Agent Platform for Local Tools & Device Automation",
    template: "%s | MegasMoves",
  },
  description:
    "MegasMoves by Amretra Dynamics connects SI agents to local tools, approved browser sessions, development workspaces and devices through a permission-aware MCP runtime. Explore preview builds and documentation.",
  applicationName: "MegasMoves",
  category: "developer tools",
  authors: [{ name: "Amretra Dynamics", url: "https://github.com/Amretra-Dynamics" }],
  creator: "Amretra Dynamics",
  publisher: "Amretra Dynamics",
  keywords: [
    "MegasMoves",
    "Megas Moves",
    "Megas WoW",
    "SI agent",
    "multi-device automation",
    "local agent runtime",
    "Model Context Protocol",
    "MCP server",
    "developer tools",
    "browser automation",
    "Windows Android Linux macOS",
    "agent coordination",
    "local-first automation",
    "Amretra Dynamics",
  ],
  openGraph: {
    type: "website",
    locale: "en_IN",
    siteName: "MegasMoves",
    url: siteUrl,
    title: "MegasMoves | Your SI Agent Across Devices",
    description:
      "Connect SI agents to approved local tools, developer workspaces and devices with MegasMCP. Explore the open-source engineering preview.",
    images: [{
      url: "/opengraph-image",
      width: 1200,
      height: 630,
      alt: "MegasMoves by Amretra Dynamics: your SI agent across devices",
    }],
  },
  twitter: {
    card: "summary_large_image",
    title: "MegasMoves | Your SI Agent Across Devices",
    description:
      "Local tools. Connected agents. Explicit permissions. Explore MegasMoves and its open-source engineering preview.",
    images: ["/opengraph-image"],
  },
  robots: { index: true, follow: true },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${display.variable} ${inter.variable} h-full`}
    >
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var saved=localStorage.getItem('megas-theme');var theme=saved==='light'||saved==='dark'?saved:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');document.documentElement.dataset.theme=theme;document.documentElement.style.colorScheme=theme;}catch(e){document.documentElement.dataset.theme='dark';document.documentElement.style.colorScheme='dark';}})();`,
          }}
        />
      </head>
      <body className="app-shell min-h-full flex flex-col">
        {children}
      </body>
    </html>
  );
}
