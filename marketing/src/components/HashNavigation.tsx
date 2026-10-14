"use client";
import { useEffect } from "react";
import { usePathname } from "next/navigation";

export default function HashNavigation() {
  const pathname = usePathname();
  useEffect(() => {
    let active = true;
    const place = () => {
      if (!active || !location.hash) return;
      let id: string;
      try { id = decodeURIComponent(location.hash.slice(1)); } catch { return; }
      document.getElementById(id)?.scrollIntoView({ block: "start", behavior: "instant" });
    };
    const ready = () => requestAnimationFrame(place);
    document.fonts.ready.then(ready);
    window.addEventListener("hashchange", ready);
    window.addEventListener("megas:downloads-ready", ready);
    return () => { active = false; window.removeEventListener("hashchange", ready); window.removeEventListener("megas:downloads-ready", ready); };
  }, [pathname]);
  return null;
}
