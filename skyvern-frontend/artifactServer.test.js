import fs from "fs";
import os from "os";
import path from "path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { app } from "./artifactServer.js";

let server;
let baseUrl;
let tempDir;

beforeAll(async () => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "skyvern-artifacts-"));
  server = app.listen(0);
  await new Promise((resolve) => server.once("listening", resolve));
  const address = server.address();
  baseUrl = `http://127.0.0.1:${address.port}`;
});

afterAll(async () => {
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
  fs.rmSync(tempDir, { recursive: true, force: true });
});

describe("artifact recording endpoint", () => {
  it("serves browser recording artifacts as WebM video", async () => {
    const recordingPath = path.join(tempDir, "recording.webm");
    fs.writeFileSync(recordingPath, Buffer.from("webm-data"));

    const response = await fetch(
      `${baseUrl}/artifact/recording?path=${encodeURIComponent(recordingPath)}`,
      { headers: { Range: "bytes=0-" } },
    );

    expect(response.status).toBe(206);
    expect(response.headers.get("content-type")).toBe("video/webm");
    expect(response.headers.get("content-range")).toBe("bytes 0-8/9");
    expect(await response.text()).toBe("webm-data");
  });
});
