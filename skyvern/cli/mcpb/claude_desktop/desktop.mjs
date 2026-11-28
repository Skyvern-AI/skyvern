import process from "node:process";
import { configureAdditionalCA } from "./desktop-ca.mjs";

try {
  configureAdditionalCA(process.env);
  const { main } = await import("./proxy.mjs");
  process.exitCode = await main();
} catch {
  process.stderr.write(
    "Skyvern API-key fallback CA/bootstrap configuration failed.\n",
  );
  process.exitCode = 1;
}
