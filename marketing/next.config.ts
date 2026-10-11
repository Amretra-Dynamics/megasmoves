import type { NextConfig } from "next";

// GitHub Pages project URLs live under /megasmoves. A configured custom domain
// serves the site from /. The deploy workflow supplies the Pages base_path.
const basePath = process.env.MEGAS_PAGES_BASE_PATH ?? "/megasmoves";

if (basePath !== "" && !/^\/[A-Za-z0-9/_-]*$/.test(basePath)) {
  throw new Error("Invalid GitHub Pages base path");
}

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  basePath,
  assetPrefix: basePath ? `${basePath}/` : undefined,
  images: { unoptimized: true },
  turbopack: { root: __dirname },
};

export default nextConfig;
