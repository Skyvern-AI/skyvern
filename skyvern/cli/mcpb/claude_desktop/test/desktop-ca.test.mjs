import test from "node:test";
import assert from "node:assert/strict";
import tls from "node:tls";
import { X509Certificate } from "node:crypto";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { configureAdditionalCA } from "../desktop-ca.mjs";

test("Desktop CA bootstrap preserves defaults and fails closed", () => {
  const original = tls.getCACertificates("default");
  const dir = mkdtempSync(join(tmpdir(), "skyvern-ca-"));
  const pem = readFileSync(new URL("fixtures/ca.pem", import.meta.url), "utf8");
  const valid = join(dir, "ca.pem");
  writeFileSync(valid, pem);
  try {
    configureAdditionalCA({});
    assert.deepEqual(tls.getCACertificates("default"), original);
    configureAdditionalCA({ SKYVERN_CA_CERTIFICATE: "" });
    assert.deepEqual(tls.getCACertificates("default"), original);
    // Deliberately no NODE_EXTRA_CA_CERTS: embedded Desktop filters it.
    configureAdditionalCA({ SKYVERN_CA_CERTIFICATE: valid });
    const added = tls.getCACertificates("default");
    const fingerprints = new Set(
      added.map((cert) => new X509Certificate(cert).fingerprint256),
    );
    for (const cert of original)
      assert.ok(fingerprints.has(new X509Certificate(cert).fingerprint256));
    assert.ok(fingerprints.has(new X509Certificate(pem).fingerprint256));
    for (const [name, content] of [
      ["empty", ""],
      ["invalid", "not a certificate"],
      [
        "private",
        "-----BEGIN " + "PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----",
      ],
      ["large", "x".repeat(1024 * 1024 + 1)],
    ]) {
      const file = join(dir, name);
      writeFileSync(file, content);
      assert.throws(
        () => configureAdditionalCA({ SKYVERN_CA_CERTIFICATE: file }),
        { message: "CONFIG_INVALID_CA" },
      );
      assert.deepEqual(tls.getCACertificates("default"), added);
    }
    assert.throws(
      () =>
        configureAdditionalCA({ SKYVERN_CA_CERTIFICATE: join(dir, "missing") }),
      { message: "CONFIG_INVALID_CA" },
    );
    assert.throws(
      () => configureAdditionalCA({ SKYVERN_CA_CERTIFICATE: "relative.pem" }),
      { message: "CONFIG_INVALID_CA" },
    );
    assert.throws(
      () => configureAdditionalCA({ SKYVERN_CA_CERTIFICATE: dir }),
      { message: "CONFIG_INVALID_CA" },
    );
  } finally {
    tls.setDefaultCACertificates(original);
    rmSync(dir, { recursive: true, force: true });
  }
});
