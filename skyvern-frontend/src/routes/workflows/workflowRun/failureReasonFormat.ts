export type FormattedFailureReason = {
  headline: string;
  detail: string | null;
};

// Failure reasons arrive as one nested blob — "for_loop block failed. failure
// reason: Failed to execute code block. Reason: Exception: … \n …" — with
// literal escape sequences. Split a short headline from the technical detail
// so the banner can lead with what failed and de-emphasize the payload.
export function formatFailureReason(raw: string): FormattedFailureReason {
  const text = raw.replace(/\\n/g, "\n").replace(/\\t/g, "  ").trim();

  const nested = text.match(/^(.{0,120}?)[.:]\s*failure reason:\s*/i);
  if (nested?.[1]) {
    const detail = text.slice(nested[0].length).trim();
    return { headline: nested[1].trim(), detail: detail || null };
  }

  // Generic shape: lead with the first sentence when a meaningful remainder
  // follows it. The whitespace lookahead keeps URLs and decimals intact.
  const sentence = text.match(/^(.{10,120}?[.!])\s+(?=\S)/);
  if (sentence?.[1]) {
    const detail = text.slice(sentence[0].length).trim();
    if (detail) {
      return { headline: sentence[1].replace(/[.!]$/, ""), detail };
    }
  }

  return { headline: text, detail: null };
}

// Gate for the detail's Show more / Show less toggle: only payloads that the
// collapsed three-line clamp would actually cut.
export function failureDetailIsLong(detail: string): boolean {
  return detail.length > 220 || detail.split("\n").length > 3;
}
