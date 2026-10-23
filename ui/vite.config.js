import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies API calls to FastAPI, so no CORS setup is needed.
const API = process.env.API_URL || "http://localhost:8000";
const paths = ["/products", "/carts", "/orders", "/admin", "/health"];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(paths.map((p) => [p, { target: API, changeOrigin: true }])),
  },
});
