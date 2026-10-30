import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const webRoot = resolve(import.meta.dirname, "dist/web");

// The Studio edge caches static assets independently of the upstream nginx
// cache headers.  The legacy portal and the React entry point historically
// shared /portal.js, so a migration could pair new HTML with old JavaScript.
// Give every HTML-to-asset and chunk-to-chunk reference a deterministic build
// identity while retaining the deliberately small publication allowlist.
function versionPublishedAssets() {
  return {
    name: "omnibioai-version-published-assets",
    apply: "build",
    closeBundle() {
      const names = ["portal.css", "embed-appearance.js", "portal.js", "shared.js"];
      const version = createHash("sha256")
        .update(names.map((name) => readFileSync(resolve(webRoot, name))).join("\0"))
        .digest("hex")
        .slice(0, 16);
      const suffix = `?v=${version}`;
      const indexPath = resolve(webRoot, "index.html");
      let html = readFileSync(indexPath, "utf8");
      for (const name of names) html = html.replaceAll(`./${name}`, `./${name}${suffix}`);
      writeFileSync(indexPath, html);

      const portalPath = resolve(webRoot, "portal.js");
      const portal = readFileSync(portalPath, "utf8").replaceAll("./shared.js", `./shared.js${suffix}`);
      writeFileSync(portalPath, portal);
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [react(), versionPublishedAssets()],
  build: {
    outDir: "dist/web",
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      input: {
        portal: resolve(import.meta.dirname, "index.html"),
        guide: resolve(import.meta.dirname, "guide.html"),
      },
      output: {
        entryFileNames: ({ name }) => `${name}.js`,
        chunkFileNames: "shared.js",
        assetFileNames: ({ names }) =>
          names?.some((name) => name.endsWith(".css")) ? "portal.css" : "[name][extname]",
      },
    },
  },
});
