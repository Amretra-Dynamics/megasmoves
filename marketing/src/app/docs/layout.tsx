"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { BookOpen, CircleDollarSign, HelpCircle, Terminal } from "lucide-react";
import Header from "@/components/Header";
import Footer from "@/components/Footer";

const menuItems = [
  { label: "Getting started", href: "/docs/getting-started", icon: HelpCircle },
  { label: "Agent reference", href: "/docs/cli-reference", icon: Terminal },
  { label: "Release status", href: "/docs/product-plans", icon: CircleDollarSign },
];

export default function DocsLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() || "";
  return (
    <div className="site-surface flex min-h-screen flex-col text-deep-ink">
      <Header />
      <div className="flex-1 overflow-hidden">
        <div className="mx-auto grid w-full max-w-7xl grid-cols-1 items-start gap-10 px-6 py-10 md:grid-cols-[260px_1fr] md:py-14">
          <aside className="space-y-6 border-b border-[#172033]/10 pb-8 md:sticky md:top-28 md:border-b-0 md:border-r md:pb-0 md:pr-6">
            <div className="flex items-center gap-2 px-1 text-sm font-semibold text-brand-orange-deep">
              <BookOpen className="h-4 w-4 text-brand-orange" /> Megas Moves docs
            </div>
            <nav aria-label="Documentation">
              <ul className="space-y-1">
                {menuItems.map((item) => {
                  const Icon = item.icon;
                  return (
                    <li key={item.href}>
                      <Link href={item.href} className={`docs-nav-link flex w-full items-center gap-2.5 rounded-full px-3 py-2 text-sm font-medium ${pathname === item.href ? "docs-nav-link--active" : ""}`}>
                        <Icon className="docs-nav-icon h-3.5 w-3.5" /> {item.label}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </nav>
          </aside>
          <div className="docs-content min-h-[500px] p-0">{children}</div>
        </div>
      </div>
      <Footer />
    </div>
  );
}
