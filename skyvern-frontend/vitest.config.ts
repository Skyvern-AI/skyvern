import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react-swc";
import path from "path";

export default defineConfig({
  plugins: [react()],
  test: {
    passWithNoTests: true,
    // Tests that transitively import the editor (e.g. workflowEditorUtils via
    // its `./nodes` barrel) touch zustand stores that read localStorage at
    // module load — run all tests under jsdom so those imports resolve. The
    // older pure-util tests still pass here; jsdom is a superset of node.
    environment: "jsdom",
    // The same chain also pulls in AxiosClient, which reads VITE_* env vars
    // and throws on undefined. Provide safe dummies so tests don't need a
    // populated .env.
    env: {
      VITE_API_BASE_URL: "http://localhost:8000/api/v1",
      VITE_ARTIFACT_API_BASE_URL: "http://localhost:9090",
      VITE_WSS_BASE_URL: "ws://localhost:8000/api/v1",
      VITE_ENVIRONMENT: "test",
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@cloud": path.resolve(__dirname, "./cloud"),
      "@eval": path.resolve(__dirname, "./eval"),
    },
  },
});
