export type FormattedFailureReason = {
  headline: string;
  detail: string | null;
};

// Block failures arrive wrapped — "login block failed. failure reason: …" or
// "navigation block terminated|timed out. Reason: …" — with literal escape
// sequences. The wrapper only repeats the block the line already names, so the
// headline leads with the inner cause; the line truncates, so detail keeps all of it.
export function formatFailureReason(raw: string): FormattedFailureReason {
  const text = raw.replace(/\\n/g, "\n").replace(/\\t/g, "  ").trim();

  const blockWrapper = text.match(
    /^.{0,120}?\bblock (?:failed|terminated|timed out)[.:]\s*(?:failure )?reason:\s*/i,
  );
  const cause = blockWrapper ? text.slice(blockWrapper[0].length).trim() : "";
  if (cause) {
    return { headline: formatFailureReason(cause).headline, detail: cause };
  }

  // Run-level wrappers ("Setup workflow failed. failure reason: …") name no
  // block, so they stay the headline.
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
