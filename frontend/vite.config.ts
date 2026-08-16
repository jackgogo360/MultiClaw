import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

function trimTrailingWhitespacePlugin() {
  return {
    name: "trim-trailing-whitespace",
    generateBundle(_options: unknown, bundle: Record<string, { type: string; code?: string; source?: string | Uint8Array }>) {
      for (const entry of Object.values(bundle)) {
        if (entry.type === "chunk" && typeof entry.code === "string") {
          entry.code = entry.code.replace(/[ \t]+$/gm, "");
          continue;
        }
        if (entry.type === "asset" && typeof entry.source === "string") {
          entry.source = entry.source.replace(/[ \t]+$/gm, "");
        }
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), trimTrailingWhitespacePlugin()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:15800",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "../src/multiclaw/static",
    emptyOutDir: true,
  },
});
