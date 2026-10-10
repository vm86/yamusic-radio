import vue from "@vitejs/plugin-vue";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => {
  // npm run dev:remote points the dev server at another radio, e.g.
  // RADIO_URL=https://radio.example.com; by default it is the local one.
  const radio = loadEnv(mode, ".", "").RADIO_URL || "http://127.0.0.1:8000";
  const proxy = { target: radio, changeOrigin: true };

  return {
    base: "./",
    plugins: [vue()],
    // The build goes straight into the Python package: the radio serves it.
    build: {
      outDir: "../yamusic_radio/web",
      emptyOutDir: true,
    },
    server: {
      // npm run dev: the page from Vite, the radio's API through the proxy.
      proxy: { "/api": proxy, "/stream": proxy },
    },
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.ts"],
    },
  };
});
