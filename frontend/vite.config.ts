import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const localOnlyRuntimeUrls: Plugin = {
  name: "chongzu-local-only-runtime-urls",
  generateBundle(_options, bundle) {
    for (const output of Object.values(bundle)) {
      if (output.type === "chunk") {
        output.code = output.code.replace(
          "https://reactjs.org/docs/error-decoder.html?invariant=",
          "about:blank#react-error?invariant=",
        );
      }
    }
  },
};

export default defineConfig({
  plugins: [react(), localOnlyRuntimeUrls],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
