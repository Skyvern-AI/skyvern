import { z } from "zod";

export function parsePowerBIDatasetId(input: string): string | null {
  const value = input.trim();
  const direct = z.uuid().safeParse(value);
  if (direct.success) return direct.data;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:") return null;
    const id = url.pathname.match(
      /\/(?:datasets|semanticmodels)\/([^/]+)(?:\/|$)/i,
    )?.[1];
    const parsed = z.uuid().safeParse(id);
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}
