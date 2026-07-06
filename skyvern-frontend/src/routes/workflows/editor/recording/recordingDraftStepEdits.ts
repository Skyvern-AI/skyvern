import {
  type RecordingDraftStep,
  type RecordingDraftStepPatch,
} from "@/store/useRecordingStore";

function isNavigationDraftStep(step: RecordingDraftStep): boolean {
  return step.action_kind === "url_change" || step.block_type === "goto_url";
}

function normalizeRecordingBlockLabel(label: string, fallback: string): string {
  let candidate = label.trim().replace(/\W+/g, "_").replace(/_+/g, "_");
  candidate = candidate.replace(/^_|_$/g, "");

  if (!candidate) {
    return fallback;
  }

  if (!/^[A-Za-z_]/.test(candidate)) {
    candidate = `${fallback}_${candidate}`;
  }

  return candidate;
}

/** Mirrors `deterministic_goto_url_label` in browser_recording/service.py. */
function deriveGotoUrlLabelFromUrl(url: string): string {
  try {
    const host = new URL(url).hostname;
    if (!host) {
      return "goto_url";
    }
    return normalizeRecordingBlockLabel(`goto_${host}`, "goto_url");
  } catch {
    return "goto_url";
  }
}

/**
 * When the operator renames "Go to wikipedia.com" → "Go to wikipedia.org", map
 * that display edit onto the goto_url block's `url` field used at commit time.
 */
function deriveUrlFromNavigationTitle(
  originalUrl: string | null | undefined,
  title: string,
): string | null {
  const trimmed = title.trim();
  if (!trimmed) {
    return null;
  }

  const goToMatch = /^go to\s+(.+)$/i.exec(trimmed);
  const destination = goToMatch?.[1]?.trim() ?? trimmed;

  try {
    if (/^https?:\/\//i.test(destination)) {
      return new URL(destination).toString();
    }

    const destinationHasPath =
      destination.includes("/") &&
      !destination.startsWith("/") &&
      destination.indexOf("/") < destination.length - 1;

    const destinationUrl = new URL(
      destination.startsWith("//")
        ? `https:${destination}`
        : `https://${destination}`,
    );

    if (!originalUrl) {
      return destinationUrl.toString();
    }

    const original = new URL(originalUrl);
    original.protocol = destinationUrl.protocol;
    original.hostname = destinationUrl.hostname;
    original.port = destinationUrl.port;

    if (destinationHasPath) {
      original.pathname = destinationUrl.pathname;
      original.search = destinationUrl.search;
      if (destinationUrl.hash) {
        original.hash = destinationUrl.hash;
      }
    }

    return original.toString();
  } catch {
    return null;
  }
}

function buildDraftStepTitlePatch(
  step: RecordingDraftStep,
  newTitle: string,
): RecordingDraftStepPatch {
  const title = newTitle.trim();
  const patch: RecordingDraftStepPatch = { title };

  if (isNavigationDraftStep(step)) {
    const url = deriveUrlFromNavigationTitle(step.url, title);
    if (url) {
      patch.url = url;
      const fallbackLabel = deriveGotoUrlLabelFromUrl(url);
      patch.label = normalizeRecordingBlockLabel(title, fallbackLabel);
    }
  }

  return patch;
}

export {
  buildDraftStepTitlePatch,
  deriveGotoUrlLabelFromUrl,
  deriveUrlFromNavigationTitle,
  isNavigationDraftStep,
  normalizeRecordingBlockLabel,
};
