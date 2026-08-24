import { Status } from "@/api/types";

const statusValues = new Set<string>(Object.values(Status));

function isKnownStatus(value: string): value is Status {
  return statusValues.has(value);
}

/**
 * Parses the shared `?status=` run-list contract — a comma-separated list of
 * statuses. Unknown and duplicate tokens are dropped so a hand-edited URL
 * cannot make the runs request 422.
 */
function parseStatusParam(raw: string | null): Array<Status> {
  if (!raw) {
    return [];
  }
  const seen = new Set<Status>();
  const out: Array<Status> = [];
  for (const token of raw.split(",")) {
    const trimmed = token.trim();
    if (trimmed === "" || !isKnownStatus(trimmed) || seen.has(trimmed)) {
      continue;
    }
    seen.add(trimmed);
    out.push(trimmed);
  }
  return out;
}

export { isKnownStatus, parseStatusParam };
