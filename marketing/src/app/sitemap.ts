import type { MetadataRoute } from "next";

export const dynamic = "force-static";

const origin = "https://megasmoves.io";

const publicRoutes = [
  "/",
  "/download/",
  "/wow/",
  "/pricing/",
  "/docs/",
  "/docs/getting-started/",
  "/docs/cli-reference/",
  "/docs/product-plans/",
  "/android-testing/",
  "/oauth-app-info/",
  "/legal/",
  "/legal/acceptable-use/",
  "/legal/ai-disclaimer/",
  "/legal/cancellation/",
  "/legal/cookies/",
  "/legal/delivery/",
  "/legal/offer-terms/",
  "/legal/refunds/",
  "/legal/service-privacy/",
  "/legal/subscription-terms/",
  "/privacy/",
  "/terms/",
] as const;

export default function sitemap(): MetadataRoute.Sitemap {
  return publicRoutes.map((path) => ({
    url: new URL(path, origin).toString(),
  }));
}
