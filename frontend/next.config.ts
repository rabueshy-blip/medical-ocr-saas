import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // pdfjs-dist (pdf.js) has a Node-only require("canvas") branch used only when
  // running server-side; it never executes in the browser but Turbopack still
  // needs it to resolve statically, so alias it away for the browser bundle.
  turbopack: {
    resolveAlias: {
      canvas: "./empty.ts",
    },
  },
};

export default nextConfig;
