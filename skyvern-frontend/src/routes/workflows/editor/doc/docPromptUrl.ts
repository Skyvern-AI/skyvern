import type { AppNode } from "../nodes";
import { getBrowserBlockUrlError } from "../browserBlockUrl";
import {
  isMissingRequiredStartUrl,
  opensPageWithoutUrl,
} from "../workflowEditorUtils";
import { safeHttpUrl } from "@/util/httpUrl";
import type { DocGraph } from "./docGraphEdits";
import { getDocProseFields } from "./docStepRegistry";

// Unicode quotes, brackets, CJK punctuation, dashes, the ellipsis and zero-width spaces never belong to a URL typed
// in prose (a copied URL carries them percent-encoded), so the match ends at the first one.
const URL_STOP =
  "\\u00AB\\u00BB\\u2018-\\u201F\\u2039\\u203A\\uFF02\\uFF07" +
  "\\u2329\\u232A\\u27E8-\\u27EF\\u3008-\\u3011\\u3014-\\u301F\\uFF08\\uFF09\\uFF3B\\uFF3D\\uFF5B\\uFF5D\\uFF5F-\\uFF63" +
  "\\u3001\\u3002\\uFF01\\uFF0C\\uFF0E\\uFF1A\\uFF1B\\uFF1F\\uFF64" +
  "\\u2013-\\u2015\\u2026\\u200B-\\u200D";
const PROMPT_URL = new RegExp(`\\bhttps?://[^\\s<>"'\`${URL_STOP}]+`, "gi");
// "]" and ")" are absent on purpose: trimUrl drops them only when the URL did not open them.
const TRAILING_PUNCTUATION = ".,;:!?*}";

const count = (text: string, char: string) => text.split(char).length - 1;

// A closing bracket ends the URL only when the URL did not open it, as in "(see https://example.com/a_(b))".
// Brackets are counted once, so a long run of closers stays linear.
function trimUrl(raw: string): string {
  const unmatched = {
    ")": count(raw, ")") - count(raw, "("),
    "]": count(raw, "]") - count(raw, "["),
  };
  let end = raw.length;
  while (end > 0) {
    const last = raw[end - 1]!;
    if (last === ")" || last === "]") {
      if (unmatched[last] <= 0) break;
      unmatched[last] -= 1;
    } else if (!TRAILING_PUNCTUATION.includes(last)) break;
    end -= 1;
  }
  return raw.slice(0, end);
}

export type DocPromptUrlOffer = {
  url: string;
  // The run check flags the step: it is the first to open a page and has no URL.
  required: boolean;
};

/** The one http(s) URL a step's prompt names, offered for its empty URL field. */
export function docPromptUrlOffer(
  graph: DocGraph,
  node: AppNode,
): DocPromptUrlOffer | null {
  // Only the instruction is read: Extract, Complete if and Terminate if text names pages the step ends on or reads, not one it starts on.
  const instruction = getDocProseFields(node)[0]?.key;
  const value = instruction
    ? (node.data as Record<string, unknown>)[instruction]
    : undefined;
  const prompt = typeof value === "string" ? value : "";
  const urls = new Map<string, string>();
  for (const match of prompt.matchAll(PROMPT_URL)) {
    const url = trimUrl(match[0]);
    const key = (!url.includes("{") && safeHttpUrl(url)) || url;
    if (!urls.has(key)) urls.set(key, url);
  }
  if (urls.size !== 1) return null;
  const [url] = [...urls.values()] as [string];
  // The URL field's own check decides what can be offered, so a click never writes a value it flags.
  if (url.includes("{") || getBrowserBlockUrlError(url) !== null) return null;
  if (!opensPageWithoutUrl(node, graph.nodes)) return null;
  return {
    url,
    required: isMissingRequiredStartUrl(graph.nodes, graph.edges, node.id),
  };
}

const MAX_SHOWN = 40;

// The scheme and host are always shown in full, so a long subdomain cannot hide the real host.
export function docShortUrl(url: string): string {
  const [, origin = url, rawPath = ""] =
    url.match(/^(https?:\/\/[^/?#]*)(.*)$/i) ?? [];
  const path = rawPath === "/" ? "" : rawPath;
  if (origin.length + path.length <= MAX_SHOWN) return origin + path;
  return `${origin}${path.slice(0, Math.max(1, MAX_SHOWN - origin.length))}…`;
}
