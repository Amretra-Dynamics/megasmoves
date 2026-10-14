import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

const display = localFont({variable: "--font-display", display: "swap", src: [{path: "./fonts/barlow-condensed-600.ttf", weight: "600", style: "normal"}, {path: "./fonts/barlow-condensed-700.ttf", weight: "700", style: "normal"}, {path: "./fonts/barlow-condensed-800.ttf", weight: "800", style: "normal"}, {path: "./fonts/barlow-condensed-900.ttf", weight: "900", style: "normal"}]});
const inter = localFont({variable: "--font-inter", display: "swap", src: [{path: "./fonts/inter-400.ttf", weight: "400", style: "normal"}, {path: "./fonts/inter-600.ttf", weight: "600", style: "normal"}, {path: "./fonts/inter-700.ttf", weight: "700", style: "normal"}, {path: "./fonts/inter-800.ttf", weight: "800", style: "normal"}, {path: "./fonts/inter-900.ttf", weight: "900", style: "normal"}]});

export const metadata: Metadata = {
  title: "Megas Moves — Local tools. Connected agents.",
  description: "Megas Moves brings local actions, tasks, runtime testing and an embedded MCP interface together.",
  keywords: [
    "megas moves",
    "megas",
    "agent",
    "ai coding agents",
    "remote coding agent",
    "coding agent streaming",
    "android coding agent",
    "web coding agent chat",
    "local coding agents"
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
