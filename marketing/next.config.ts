import type { NextConfig } from "next";
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  basePath: "/megasmoves",
  assetPrefix: "/megasmoves/",
  images: { unoptimized: true },
  turbopack: { root: __dirname },
};
export default nextConfig;
