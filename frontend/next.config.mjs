/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  eslint: {
    // We rely on CI for lint; skip during Docker build.
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
