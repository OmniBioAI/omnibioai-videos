import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "./",
  plugins: [react()],
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
