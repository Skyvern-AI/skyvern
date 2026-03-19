import { execSync } from "child_process";
import path from "path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 8080,
  },
  preview: {
    port: 8080,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  define: {
    __APP_VERSION__: JSON.stringify(
      process.env.APP_VERSION ||
        (() => {
          try {
            return execSync("git rev-parse HEAD").toString().trim();
          } catch {
            return "development";
          }
        })(),
    ),
  },
});
