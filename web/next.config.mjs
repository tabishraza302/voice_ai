/** @type {import('next').NextConfig} */
const platformApiOrigin =
  process.env.PLATFORM_API_ORIGIN || "http://127.0.0.1:8000";

const nextConfig = {
  transpilePackages: ["lucide-react"],
  async rewrites() {
    return [
      {
        source: "/api/v1/catalog",
        destination: `${platformApiOrigin}/catalog`,
      },
      {
        source: "/api/v1/:path*",
        destination: `${platformApiOrigin}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
