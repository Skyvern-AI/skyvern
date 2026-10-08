import { isTemplateExpression } from "@/util/googleSheetsUrl";

import type { AppNode } from "./nodes";

const INVALID_URL_MESSAGE = "Enter a full web address, like example.com.";
const HOST_PORT_MESSAGE =
  "Add https:// before an address with a port, like https://example.com:8080.";
const UNSUPPORTED_SCHEME_MESSAGE =
  "Only http:// and https:// addresses can be opened.";

const IPV4_HOST = /^\d{1,3}(\.\d{1,3}){3}$/;
// The URL parser has already lowercased and punycoded the host.
const TOP_LEVEL_DOMAIN = /^([a-z]{2,63}|xn--[a-z0-9-]{1,59})$/;

function isPlausibleHost(host: string, typed: string, hasScheme: boolean) {
  if (host.startsWith("[")) return true;
  // The URL parser turns bare numbers into IPv4 ("10" becomes "0.0.0.10"), so
  // only trust an IPv4 host that was typed out in full.
  if (IPV4_HOST.test(host)) return typed.includes(host);
  if (!host.includes(".")) {
    // A bare word like "example" is a typo; an explicit http://service:port
    // stays valid for self-hosted setups.
    return host === "localhost" || hasScheme;
  }
  const labels = host.split(".");
  return (
    labels.every((label) => label !== "") &&
    TOP_LEVEL_DOMAIN.test(labels[labels.length - 1]!)
  );
}

// The backend prepends https:// to a scheme-less URL and only accepts http(s)
// (url_validators._prepend_scheme), so match that before judging the host.
export function getBrowserBlockUrlError(url: string): string | null {
  const trimmed = url.trim();
  if (trimmed === "" || isTemplateExpression(trimmed)) return null;

  const scheme = trimmed.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):/)?.[1];
  if (scheme && !["http", "https"].includes(scheme.toLowerCase())) {
    // Python's urlparse reads "example.com:8080" as scheme "example.com", so the
    // backend rejects a host:port with no scheme too; say how to fix it.
    return /^[^:]+:\d+(\/|$)/.test(trimmed)
      ? HOST_PORT_MESSAGE
      : UNSUPPORTED_SCHEME_MESSAGE;
  }

  let parsed: URL;
  try {
    parsed = new URL(scheme ? trimmed : `https://${trimmed}`);
  } catch {
    return INVALID_URL_MESSAGE;
  }

  return isPlausibleHost(parsed.hostname, trimmed.toLowerCase(), !!scheme)
    ? null
    : INVALID_URL_MESSAGE;
}

// File Download is left out: its URL can also be a bare Google Drive file id.
export function getNodeBrowserUrlError(node: AppNode): string | null {
  switch (node.type) {
    case "task":
    case "navigation":
    case "action":
    case "extraction":
    case "login":
    case "url":
      return getBrowserBlockUrlError(node.data.url);
    default:
      return null;
  }
}
