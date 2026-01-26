#!/usr/bin/env node
import { spawn } from "child_process";
import http from "http";
import { fileURLToPath } from "url";
import { dirname, resolve } from "path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const WEB_DIR = resolve(__dirname, "../web");
const PORT = 9010;

let server;

function startServer() {
  return new Promise((resolve, reject) => {
    const handler = (req, res) => {
      import("http-server").then(({ default: httpServer }) => {
        const serve = httpServer.createServer({ root: WEB_DIR });
        serve.emit("request", req, res);
      });
    };

    // Use http-server via spawn instead
    server = spawn("npx", ["http-server", WEB_DIR, "-p", PORT, "--silent"], {
      stdio: "inherit",
    });

    // Wait a bit for server to start
    setTimeout(() => {
      http
        .get(`http://localhost:${PORT}`, () => {
          console.log(`Web server started at http://localhost:${PORT}`);
          resolve();
        })
        .on("error", () => {
          // Retry after a short delay
          setTimeout(() => {
            http
              .get(`http://localhost:${PORT}`, () => {
                console.log(`Web server started at http://localhost:${PORT}`);
                resolve();
              })
              .on("error", reject);
          }, 500);
        });
    }, 500);
  });
}

function runTests(args) {
  return new Promise((resolve, reject) => {
    const testProcess = spawn("npx", ["tsx", ...args], {
      stdio: "inherit",
    });

    testProcess.on("close", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`Tests failed with exit code ${code}`));
      }
    });
  });
}

async function main() {
  const testArgs = process.argv.slice(2);

  if (testArgs.length === 0) {
    console.error("Usage: npm test <test-file> [test-name]");
    console.error("Example: npm test test_simple_actions.ts testClicks");
    process.exit(1);
  }

  try {
    await startServer();
    await runTests(testArgs);
    console.log("Tests completed successfully");
  } catch (error) {
    console.error("Error:", error.message);
    process.exit(1);
  } finally {
    if (server) {
      server.kill();
      console.log("Web server stopped");
    }
  }
}

main();
