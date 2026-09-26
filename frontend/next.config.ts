import createNextIntlPlugin from "next-intl/plugin";
import type { NextConfig } from "next";

const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

const nextConfig: NextConfig = {
  // Ship the minimum into the container: `standalone` traces the modules
  // actually imported and copies only those, which turns a ~400 MB
  // node_modules into a runtime image measured in tens of megabytes.
  output: "standalone",

  // The build fails on a type error rather than shipping one. Next allows
  // this to be waved through; for software that records clinical decisions,
  // "it compiled with warnings" is not a release.
  //
  // Next 16 dropped the `eslint` build option along with `next lint`, so
  // linting is a separate step — `npm run verify` runs both, and CI should
  // use that rather than `next build` alone.
  typescript: { ignoreBuildErrors: false },

  // No `X-Powered-By: Next.js`. Free information for anyone scanning.
  poweredByHeader: false,

  // React's strict double-render in development, which surfaces effects that
  // are not idempotent — the class of bug that shows up in production as a
  // duplicate token or a doubled charge.
  reactStrictMode: true,

  experimental: {
    // Import only the icons a screen uses. `lucide-react` is a barrel of
    // ~1,500 exports; without this, one icon can pull the whole set into a
    // bundle staff download over hospital wifi.
    optimizePackageImports: ["lucide-react", "@tanstack/react-query"],
  },
};

export default withNextIntl(nextConfig);
