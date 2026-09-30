import { strFromU8, unzipSync } from "fflate";
import {
  isMap,
  isScalar,
  isSeq,
  parse as parseYAML,
  parseAllDocuments,
  parseDocument,
  stringify as convertToYAML,
} from "yaml";

import type { RunEngine } from "@/api/types";
import { blockEngineForWorkflow } from "./editor/workflowEditorUtils";

function isJsonString(str: string): boolean {
  try {
    JSON.parse(str);
  } catch {
    return false;
  }
  return true;
}

// The backend parses with PyYAML (YAML 1.1), which reads plain `yes`, `on`, `1:30` or `1_000` as a bool or an
// int; a 1.1 dump quotes those strings.
function toBackendYaml(value: unknown): string {
  return convertToYAML(value, { version: "1.1" });
}

// Bulk export bundles N workflows into one file: multi-document YAML (docs
// joined by `---`) or a top-level JSON array. Split it back into one YAML
// string per workflow so each can be POSTed as its own workflow. A file that
// holds a single workflow passes through unchanged.
export function expandFileToWorkflowYamls(text: string): string[] {
  if (isJsonString(text)) {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed.map((workflow) => toBackendYaml(workflow));
    }
    return [toBackendYaml(parsed)];
  }
  const documents = parseAllDocuments(text);
  for (const document of documents) {
    // parseAllDocuments recovers from syntax errors instead of throwing, so
    // toJS() would hand back a silently-truncated object. Reject the file so it
    // falls back to raw-text passthrough and the backend rejects it, rather
    // than importing a partial workflow. An intentionally-empty trailing `---`
    // document has no errors and is dropped by the null filter below.
    const [error] = document.errors;
    if (error) {
      throw new Error(error.message);
    }
  }
  // Each workflow is its document's own source text, never a re-dump, so every value reaches the backend as written.
  const workflows: string[] = [];
  let start = 0;
  for (const document of documents) {
    const end = document.range[2];
    const value = document.toJS();
    if (value !== null && typeof value === "object") {
      workflows.push(text.slice(start, end));
    }
    start = end;
  }
  if (workflows.length <= 1) {
    return [text];
  }
  return workflows;
}

export type ArchiveEntry = { name: string; text: string };

// unzipSync runs synchronously on the UI thread; cap size/entry count so an
// oversized archive fails fast instead of hanging the tab.
const MAX_ZIP_BYTES = 20 * 1024 * 1024;
const MAX_ZIP_ENTRIES = 200;

function isWorkflowArchiveEntry(name: string): boolean {
  if (name.startsWith("__MACOSX/")) {
    return false;
  }
  const basename = name.split("/").pop() ?? "";
  if (basename.startsWith(".")) {
    return false; // .DS_Store, AppleDouble "._*" resource forks
  }
  return /\.(ya?ml|json)$/i.test(name);
}

// Bulk export (N > 1) writes one ZIP with one file per agent. Unpack it back
// into individual entries so each can go through the normal single-file
// import path (which itself calls expandFileToWorkflowYamls per entry).
export function unzipArchive(bytes: Uint8Array): ArchiveEntry[] {
  if (bytes.length > MAX_ZIP_BYTES) {
    throw new Error(
      `Archive is too large to import (max ${MAX_ZIP_BYTES / (1024 * 1024)}MB).`,
    );
  }
  const unzipped = unzipSync(bytes);
  const entries = Object.entries(unzipped)
    .filter(([name, data]) => isWorkflowArchiveEntry(name) && data.length > 0)
    .map(([name, data]) => ({ name, text: strFromU8(data) }));
  if (entries.length > MAX_ZIP_ENTRIES) {
    throw new Error(
      `Archive has too many files to import (max ${MAX_ZIP_ENTRIES}).`,
    );
  }
  return entries;
}

export function extractTitleFromYaml(yaml: string): string | null {
  try {
    const parsed = parseYAML(yaml);
    if (parsed && typeof parsed === "object" && "title" in parsed) {
      const title = (parsed as { title?: unknown }).title;
      if (typeof title === "string" && title.trim().length > 0) {
        return title.trim();
      }
    }
  } catch {
    return null;
  }
  return null;
}

type Span = [number, number];

// Returns the byte span of each `engine: skyvern-1.0` line, or null when one sits where it cannot be cut
// without rewriting its neighbours (flow style, or sharing a line with other content).
function legacyEngineSpans(text: string, blocks: unknown): Span[] | null {
  if (!isSeq(blocks)) {
    return [];
  }
  const spans: Span[] = [];
  for (const block of blocks.items) {
    if (!isMap(block)) {
      continue;
    }
    for (const [index, pair] of block.items.entries()) {
      const { key, value } = pair;
      if (
        !isScalar(key) ||
        key.value !== "engine" ||
        !isScalar(value) ||
        typeof value.value !== "string" ||
        blockEngineForWorkflow(value.value as RunEngine, null) !== null
      ) {
        continue;
      }
      if (blocks.flow || block.flow || !key.range || !value.range) {
        return null;
      }
      const lineStart = text.lastIndexOf("\n", key.range[0] - 1) + 1;
      const newline = text.indexOf("\n", value.range[1]);
      const lineEnd = newline === -1 ? text.length : newline + 1;
      if (
        !/^[ \t\r]*(#[^\n]*)?\n?$/.test(text.slice(value.range[1], lineEnd))
      ) {
        return null;
      }
      const prefix = text.slice(lineStart, key.range[0]);
      if (/^\s*$/.test(prefix)) {
        spans.push([lineStart, lineEnd]);
        continue;
      }
      // `- engine: skyvern-1.0` opens its block: cut up to the next key so it takes the `- `.
      const next = block.items[index + 1]?.key;
      if (
        !/^\s*-\s+$/.test(prefix) ||
        !isScalar(next) ||
        !next.range ||
        !/^\s*$/.test(text.slice(lineEnd, next.range[0]))
      ) {
        return null;
      }
      spans.push([key.range[0], next.range[0]]);
    }
    const nested = legacyEngineSpans(text, block.get("loop_blocks", true));
    if (nested === null) {
      return null;
    }
    spans.push(...nested);
  }
  return spans;
}

// Cuts only the legacy engine lines out of the original text, so every other byte reaches the backend as written.
export function stripLegacyEngineFromYaml(yaml: string): string {
  try {
    const document = parseDocument(yaml);
    if (document.errors.length > 0 || !isMap(document.contents)) {
      return yaml;
    }
    const definition = document.contents.get("workflow_definition", true);
    const spans = isMap(definition)
      ? legacyEngineSpans(yaml, definition.get("blocks", true))
      : null;
    if (!spans) {
      return yaml;
    }
    return spans
      .sort((a, b) => b[0] - a[0])
      .reduce((text, [from, to]) => text.slice(0, from) + text.slice(to), yaml);
  } catch {
    return yaml;
  }
}
