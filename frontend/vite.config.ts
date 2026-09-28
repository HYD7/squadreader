/// <reference types="node" />
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies every backend path so the SPA can use relative
// URLs both in dev (Vite on :5173) and in prod (httpsrv serving dist/
// on :8081 behind nginx). One config, no env switching.
const PROXY_TARGET = process.env.SQREADER_BACKEND ?? "http://127.0.0.1:8080";
const proxyPath = {
  target: PROXY_TARGET,
  changeOrigin: true,
  ws: false,
  configure: (proxy: { on: (ev: string, cb: (...a: unknown[]) => void) => void }) => {
    proxy.on("proxyReq", () => {});
  },
};

// Zero Zone build (`vite build --mode zerozone`, see `npm run build:zerozone`):
// a replay-only viewer that Zero Zone serves locally under /tekrar/. It has its
// own entry page (index.zerozone.html → src/zz/main.tsx), which carries no
// analytics and never imports the live map, stats pages or recording picker.
// The page is emitted as `index.html` inside dist-zerozone/. The default build
// is untouched.
function zeroZoneHtmlName(): Plugin {
  return {
    name: "zerozone-html-name",
    enforce: "post",
    generateBundle(_opts, bundle) {
      const page = bundle["index.zerozone.html"];
      if (!page) throw new Error("index.zerozone.html missing from bundle");
      delete bundle["index.zerozone.html"];
      page.fileName = "index.html";
      bundle["index.html"] = page;
    },
  };
}

export default defineConfig(({ mode }) => {
  const zz = mode === "zerozone";
  return {
    // Relative base so the built SPA works whether mounted at `/`,
    // `/sqr/viewer-next/`, `/sqr1/viewer-next/`, `/tekrar/`, or any nginx
    // prefix. All asset URLs become `./assets/...`, resolved against the
    // current document URL by the browser.
    base: "./",
    plugins: [react(), ...(zz ? [zeroZoneHtmlName()] : [])],
    build: {
      outDir: zz ? "dist-zerozone" : "dist",
      // No source maps in the committed prod build — contributors debug via the
      // dev server (npm run dev), which has them.
      sourcemap: false,
      assetsDir: "assets",
      ...(zz ? { rollupOptions: { input: "index.zerozone.html" } } : {}),
    },
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/api":     proxyPath,
        "/health":  proxyPath,
        "/icons":   proxyPath,
        "/sqmaps":  proxyPath,
      },
    },
  };
});
