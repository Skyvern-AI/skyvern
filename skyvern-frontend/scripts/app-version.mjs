import { execSync } from "child_process";

// Single source for the build version: the `__APP_VERSION__` define (SDK
// `version`) and the Datadog upload's `--release-version` must derive from this.
export function resolveAppVersion(env = process.env) {
  if (env.VERCEL_GIT_COMMIT_SHA) {
    return env.VERCEL_GIT_COMMIT_SHA;
  }
  try {
    return execSync("git rev-parse HEAD").toString().trim();
  } catch {
    return "development";
  }
}
