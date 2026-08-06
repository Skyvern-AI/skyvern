import { readFileSync } from "node:fs";
import path from "path";
import { defineConfig, loadEnv } from "vite";
import type { Connect, Plugin } from "vite";
import react from "@vitejs/plugin-react-swc";

import {
  createUiSessionHandler,
  createUiSessionNonceManager,
  issueUiSessionNonceCookie,
} from "./localServer.js";
import { resolveAppVersion } from "./scripts/app-version.mjs";

// https://vitejs.dev/config/
const devPort = process.env.VITE_DEV_PORT
  ? parseInt(process.env.VITE_DEV_PORT, 10)
  : 8080;

const UI_SESSION_DEFAULT_API_BASE_URL = "http://localhost:8000/api/v1";
const UI_SESSION_REQUEST_TIMEOUT_MS = 10_000;

type UiSessionEnvironment = Record<string, string | undefined>;

type UiSessionDevPluginOptions = {
  env: UiSessionEnvironment;
  fetchImpl?: typeof fetch;
  logger?: Pick<Console, "error">;
  requestTimeoutMs?: number;
};

function resolveOrganizationApiKey(env: UiSessionEnvironment): string | null {
  const key = env.SKYVERN_API_KEY?.trim() || env.VITE_SKYVERN_API_KEY?.trim();
  return key || null;
}

function resolveServerApiBaseUrl(env: UiSessionEnvironment): string {
  return (
    env.SKYVERN_API_BASE_URL?.trim() ||
    env.VITE_API_BASE_URL?.trim() ||
    UI_SESSION_DEFAULT_API_BASE_URL
  );
}

export function createUiSessionDevPlugin({
  env,
  fetchImpl = fetch,
  logger = console,
  requestTimeoutMs = UI_SESSION_REQUEST_TIMEOUT_MS,
}: UiSessionDevPluginOptions): Plugin {
  const apiBaseUrl = resolveServerApiBaseUrl(env);
  const organizationApiKey = resolveOrganizationApiKey(env);
  const nonceManager = createUiSessionNonceManager();
  const serveUiSession = createUiSessionHandler({
    apiBaseUrl,
    organizationApiKey,
    fetchImpl,
    logger,
    requestTimeoutMs,
    nonceManager,
  });

  const middleware: Connect.NextHandleFunction = async (request, response) => {
    await serveUiSession(request, response);
  };

  return {
    name: "ui-session-dev",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        issueUiSessionNonceCookie(request, response, nonceManager);
        next();
      });
      server.middlewares.use("/ui-session", middleware);
    },
  };
}

function hasSourceMapPruningStep() {
  const packageJson = JSON.parse(
    readFileSync(new URL("./package.json", import.meta.url), "utf8"),
  );
  return packageJson.scripts?.build?.includes("npm run datadog:sourcemaps");
}

async function createSentryPlugin() {
  try {
    const { sentryVitePlugin } = await import("@sentry/vite-plugin");
    return sentryVitePlugin({
      org: "skyvern",
      project: "javascript-react",
    });
  } catch (error) {
    const missingDependency =
      error instanceof Error &&
      "code" in error &&
      ["ERR_MODULE_NOT_FOUND", "MODULE_NOT_FOUND"].includes(error.code) &&
      error.message.includes("@sentry/vite-plugin");
    if (!missingDependency) {
      throw error;
    }
    return { name: "optional-sentry-noop" };
  }
}

export default defineConfig(async ({ mode }) => {
  const env = {
    ...loadEnv(mode, process.cwd(), ""),
    ...process.env,
  };

  return {
    plugins: [
      react(),
      createUiSessionDevPlugin({ env }),
      await createSentryPlugin(),
    ],

    server: {
      port: devPort,
      allowedHosts: process.env.VITE_ALLOWED_HOSTS
        ? process.env.VITE_ALLOWED_HOSTS.split(",")
        : [],
    },

    preview: {
      port: devPort,
    },

    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
        "@cloud": path.resolve(__dirname, "./cloud"),
        "@eval": path.resolve(__dirname, "./eval"),
      },
    },

    define: {
      __APP_VERSION__: JSON.stringify(resolveAppVersion()),
    },

    build: {
      // "hidden" emits maps for the Datadog upload but omits the
      // sourceMappingURL comment; the upload script then prunes them from dist so
      // they are never served. See scripts/upload-datadog-sourcemaps.mjs.
      sourcemap: hasSourceMapPruningStep() ? "hidden" : false,
    },
  };
});
