import type { Metadata } from "next";
import { Barlow_Condensed, Inter } from "next/font/google";
import "./globals.css";

const display = Barlow_Condensed({
  variable: "--font-display",
  subsets: ["latin"],
  weight: ["600", "700", "800", "900"],
});

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  weight: ["400", "600", "700", "800", "900"],
});

const publicAssetPrefix = process.env.NEXT_PUBLIC_MEGAS_PAGES_BASE_PATH ?? "/megasmoves";

export const metadata: Metadata = {
  icons: {
    icon: [{ url: `${publicAssetPrefix}/megas-logo.png`, type: "image/png" }],
    apple: [{ url: `${publicAssetPrefix}/megas-logo.png`, type: "image/png" }],
  },
  title: "MegasMoves | Your always-on SI agent for all your devices",
  description: "MegasMoves connects SI agents with device and workspace capabilities through MCP and permission-aware execution.",
  keywords: [
    "megas moves",
    "megas",
    "agent",
    "SI agents",
    "cross-device automation",
    "MCP execution",
    "Android integration",
    "browser automation",
    "local-first software"
  ],
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
