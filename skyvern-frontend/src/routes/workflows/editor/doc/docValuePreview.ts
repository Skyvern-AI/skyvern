import { filenameForDownloadedFileUrl } from "../../workflowRun/blockDownloadedFiles";
import { docShortUrl, MAX_SHOWN } from "./docPromptUrl";

export type DocValuePreview = { text: string; title?: string; href?: string };

// A URL with a {{ }} template in it is not a real address yet, so it stays text.
const WEB_URL = /^https?:\/\/[^\s{}]+$/i;
// Any other scheme (s3:// from an upload, gs://, ...) is named but never linked.
const STORAGE_URI = /^([a-z][a-z0-9+.-]*):\/\/([^\s/{}]+)((?:\/[^\s{}]*)?)$/i;
const MAX_TITLE = 2000;

function webUrl(value: unknown): URL | null {
  if (typeof value !== "string" || !WEB_URL.test(value)) return null;
  try {
    return new URL(value);
  } catch {
    return null;
  }
}

function decoded(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function pathSegments(path: string): string[] {
  return path.split("/").filter(Boolean).map(decoded);
}

function storagePreview(text: string): DocValuePreview | null {
  const [, scheme = "", host = "", path = ""] = text.match(STORAGE_URI) ?? [];
  if (!scheme || /^https?$/i.test(scheme)) return null;
  return {
    text: pathSegments(path).pop() ?? `${scheme}://${host}`,
    title: text,
  };
}

function cappedTitle(title: string): string {
  return title.length > MAX_TITLE ? `${title.slice(0, MAX_TITLE)}…` : title;
}

function jsonPreview(value: object): DocValuePreview {
  const title = cappedTitle(JSON.stringify(value, null, 2));
  if (Array.isArray(value)) {
    return {
      text: `[${value.length} item${value.length === 1 ? "" : "s"}]`,
      title,
    };
  }
  const keys = Object.keys(value);
  const shown = keys.slice(0, 2);
  if (keys.length > shown.length) shown.push(`+${keys.length - 2}`);
  return { text: `{${shown.join(", ")}}`, title };
}

function parsedJson(value: string): object | null {
  if (!/^\s*[[{]/.test(value)) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    return typeof parsed === "object" && parsed !== null ? parsed : null;
  } catch {
    return null;
  }
}

export function docInputValuePreview(value: unknown): DocValuePreview | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "object") return jsonPreview(value);
  const text = String(value);
  const url = webUrl(text);
  if (url) {
    const segments = pathSegments(url.pathname);
    const middle = segments.length > 1 ? "/…" : "";
    const last = segments.length > 0 ? `/${segments[segments.length - 1]}` : "";
    return { text: `${url.host}${middle}${last}`, title: text };
  }
  const storage = storagePreview(text);
  if (storage) return storage;
  const json = parsedJson(text);
  if (json) return jsonPreview(json);
  return { text: text.replace(/^https?:\/\//, "") };
}

function cellUrlText(url: URL, text: string): string {
  if (url.searchParams.has("artifact_name")) {
    return filenameForDownloadedFileUrl(text);
  }
  const last = pathSegments(url.pathname).pop();
  if (!last || !/\.[a-z0-9]{1,8}$/i.test(last)) return docShortUrl(text);
  // Two links that differ only by query must not read the same.
  const named = `${last}${url.search}`;
  return named.length <= MAX_SHOWN ? named : docShortUrl(text);
}

// A result cell has no editor or copy action, so a small object stays readable as compact JSON.
export function docCellPreview(value: unknown): DocValuePreview {
  if (value === null || value === undefined || value === "") {
    return { text: "–" };
  }
  if (typeof value === "object") {
    const compact = JSON.stringify(value);
    return compact.length <= MAX_SHOWN ? { text: compact } : jsonPreview(value);
  }
  const text = String(value);
  const url = webUrl(text);
  if (url) return { text: cellUrlText(url, text), title: text, href: text };
  return storagePreview(text) ?? { text };
}
