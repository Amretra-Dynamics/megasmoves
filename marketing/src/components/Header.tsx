"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ArrowRight, Menu, Moon, Sun, X } from "lucide-react";
import { useState } from "react";

type Theme = "dark" | "light";

const navigation = [
  { href: "/#features", label: "Features" },
  { href: "/wow", label: "Megas WoW" },
  { href: "/download", label: "Downloads" },
  { href: "https://github.com/Amretra-Dynamics/megasmoves", label: "GitHub" },
  { href: "https://t.me/megasmovesSI", label: "Community" },
  { href: "/docs", label: "Docs" },
];

function setDocumentTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  localStorage.setItem("megas-theme", theme);
}

export default function Header() {
  const pathname = usePathname() || "/";
  const [menuOpen, setMenuOpen] = useState(false);

  const toggleTheme = () => {
    const nextTheme: Theme =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    setDocumentTheme(nextTheme);
  };

  return (
    <header className="site-header">
      <div className="site-header__inner">
        <Link href="/" className="site-brand" aria-label="Megas Moves home">
          <span className="site-brand__mark" aria-hidden="true">
            <Image
              src="/megas-logo-4k.png"
              alt=""
              width={44}
              height={44}
              priority
            />
          </span>
          <span className="site-brand__name">Megas Moves</span>
        </Link>

        <nav className="site-nav" aria-label="Primary navigation">
          {navigation.map((item) => {
            const route = item.href.split("#")[0];
            const active = item.href === "/#features"
              ? pathname === "/"
              : !item.href.includes("#") && (pathname === route || pathname.startsWith(`${route}/`));
            return (
              <Link
                key={item.href}
                href={item.href}
                className={active ? "is-active" : undefined}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="site-header__actions">
          <button
            type="button"
            className="icon-command"
            onClick={toggleTheme}
            aria-label="Toggle color theme"
            title="Toggle color theme"
          >
            <Sun className="theme-icon theme-icon--sun" aria-hidden="true" />
            <Moon className="theme-icon theme-icon--moon" aria-hidden="true" />
          </button>
          <Link href="/docs/getting-started" className="button button--quiet site-header__login">
            Setup
          </Link>
          <Link href="/download" className="button button--primary site-header__cta">
            Get started free
            <ArrowRight aria-hidden="true" />
          </Link>
          <button
            type="button"
            className="icon-command site-header__menu"
            onClick={() => setMenuOpen((open) => !open)}
            aria-expanded={menuOpen}
            aria-controls="mobile-navigation"
            aria-label={menuOpen ? "Close navigation" : "Open navigation"}
          >
            {menuOpen ? <X aria-hidden="true" /> : <Menu aria-hidden="true" />}
          </button>
        </div>
      </div>

      {menuOpen ? (
        <nav id="mobile-navigation" className="mobile-nav" aria-label="Mobile navigation">
          {navigation.map((item) => (
            <Link key={item.href} href={item.href} onClick={() => setMenuOpen(false)}>
              {item.label}
              <ArrowRight aria-hidden="true" />
            </Link>
          ))}
          <Link href="/docs/getting-started" onClick={() => setMenuOpen(false)}>
            Setup
            <ArrowRight aria-hidden="true" />
          </Link>
          <Link href="/download" onClick={() => setMenuOpen(false)}>
            Get started free
            <ArrowRight aria-hidden="true" />
          </Link>
        </nav>
      ) : null}
    </header>
  );
}
