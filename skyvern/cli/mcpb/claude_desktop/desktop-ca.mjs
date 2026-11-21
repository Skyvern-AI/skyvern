// @ts-check
import { openSync, fstatSync, readSync, closeSync, constants } from "node:fs";
import { isAbsolute } from "node:path";
import { X509Certificate } from "node:crypto";
import tls from "node:tls";
// These public APIs postdate the minimum development Node type definitions.
const trust =
  /** @type {typeof tls & {getCACertificates: (type: string) => string[], setDefaultCACertificates: (certs: string[]) => void}} */ (
    tls
  );

/** Add only the explicitly configured CA to this runtime's existing trust.
 * Desktop's embedded Node filters NODE_EXTRA_CA_CERTS, so use a dedicated key.
 * @param {Record<string, string | undefined>} env
 */
export function configureAdditionalCA(env) {
  const file = env.SKYVERN_CA_CERTIFICATE;
  if (!file) return;
  let fd;
  try {
    if (!isAbsolute(file)) throw new Error();
    fd = openSync(file, constants.O_RDONLY | (constants.O_NONBLOCK ?? 0));
    const info = fstatSync(fd);
    if (!info.isFile() || info.size < 1 || info.size > 1024 * 1024)
      throw new Error();
    const buffer = Buffer.alloc(1024 * 1024 + 1);
    let length = 0;
    while (length < buffer.length) {
      const count = readSync(fd, buffer, length, buffer.length - length, null);
      if (count === 0) break;
      length += count;
    }
    if (length > 1024 * 1024) throw new Error();
    const pem = buffer.subarray(0, length).toString("utf8");
    const certs = pem.match(
      /-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g,
    );
    if (
      !certs?.length ||
      pem
        .replace(
          /-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g,
          "",
        )
        .trim()
    )
      throw new Error();
    for (const cert of certs) new X509Certificate(cert);
    if (
      typeof trust.getCACertificates !== "function" ||
      typeof trust.setDefaultCACertificates !== "function"
    )
      throw new Error();
    trust.setDefaultCACertificates([
      ...trust.getCACertificates("default"),
      ...certs,
    ]);
  } catch {
    throw new Error("CONFIG_INVALID_CA");
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}
