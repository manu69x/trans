/** @type {import('next').NextConfig} */

// Local-only MVP — nessun dominio esterno. Il browser parla SOLO con l'origine
// Next: /api/v1/* è inoltrato al backend FastAPI e /api/gateway/* al Gateway
// Proxy (route handler), così non serve CORS e nessun host è esposto al client.
const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";
const path = require("path");

const nextConfig = {
  reactStrictMode: true,
  env: {
    NEXT_PUBLIC_API_BASE: process.env.NEXT_PUBLIC_API_BASE || "",
  },
  // explicit webpack alias: some build environments don't pick up the
  // tsconfig "paths" mapping for "@/..." during production builds
  webpack: (config) => {
    config.resolve.alias["@"] = require("path").resolve(__dirname, "src");
    return config;
  },
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${BACKEND_URL}/api/v1/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
