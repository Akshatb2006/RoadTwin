import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next's dev server blocks cross-origin requests to /_next/* by default, and
  // "cross-origin" includes reaching the same machine by a different hostname.
  // Opening the dashboard at 127.0.0.1 or over the LAN (a laptop driving a
  // projector, a judge on another device) 403s every JS chunk, which surfaces
  // as "non-JavaScript MIME type" and a page that renders its shell but never
  // boots. Allowlisting the local aliases makes the demo work from any of them.
  allowedDevOrigins: [
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "*.local",
    "192.168.*.*",
    "10.*.*.*",
    "172.16.*.*",
  ],
};

export default nextConfig;
