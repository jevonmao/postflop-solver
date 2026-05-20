/** @type {import('next').NextConfig} */
const nextConfig = {
  experimental: {
    // Allow file-system reads outside the project directory (../data).
    outputFileTracingIncludes: { '/**': ['../data/**'] },
  },
};

export default nextConfig;
