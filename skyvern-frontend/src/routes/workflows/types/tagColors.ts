import type { TagValue } from "./tagTypes";

// Mirror of skyvern/forge/sdk/workflow/models/validators.py TAG_COLOR_PALETTE.
// Keep the names AND order in sync with the backend: the order is the random-pick
// pool, and any name the backend would 422 must be one the frontend can render.
export const TAG_COLOR_PALETTE = [
  "gray",
  "red",
  "orange",
  "amber",
  "yellow",
  "green",
  "teal",
  "blue",
  "cyan",
  "indigo",
  "purple",
  "pink",
] as const;

export type PaletteColorName = (typeof TAG_COLOR_PALETTE)[number];

// Solid swatch fill per name, for the color picker dots.
const SWATCH_CLASSES: Record<PaletteColorName, string> = {
  gray: "bg-gray-400",
  red: "bg-red-500",
  orange: "bg-orange-500",
  amber: "bg-amber-500",
  yellow: "bg-yellow-400",
  green: "bg-green-500",
  teal: "bg-teal-500",
  blue: "bg-blue-500",
  cyan: "bg-cyan-500",
  indigo: "bg-indigo-500",
  purple: "bg-purple-500",
  pink: "bg-pink-500",
};

const PALETTE_SET = new Set<string>(TAG_COLOR_PALETTE);

export function isPaletteColorName(value: unknown): value is PaletteColorName {
  return typeof value === "string" && PALETTE_SET.has(value);
}

export function paletteSwatchClass(color: PaletteColorName): string {
  return SWATCH_CLASSES[color];
}

// Solid dot class (bg-X-500) for a grouped tag's palette color; "" for anything
// outside the palette so the caller renders no dot. The chip surface stays neutral
// per the AC — a leading color indicator, not a tinted badge fill.
export function paletteDotClass(color: string | null | undefined): string {
  return isPaletteColorName(color) ? paletteSwatchClass(color) : "";
}

export function randomPaletteColor(): PaletteColorName {
  const index = Math.floor(Math.random() * TAG_COLOR_PALETTE.length);
  return TAG_COLOR_PALETTE[index] ?? TAG_COLOR_PALETTE[0];
}

// Color identity is per grouped (key, value). Standalone labels have no key and
// are never colored, so they never produce a map entry. The space separator is
// unambiguous: keys match ^[A-Za-z0-9][A-Za-z0-9_.-]*$ and can't contain a space,
// so everything up to the first space is the key (matches termDedupeKey).
export function tagColorKey(key: string, value: string): string {
  return `${key} ${value}`;
}

// Map of (key, value) -> palette color name. A real Map (not an object) so a
// user-controlled key like "constructor" can't resolve to an Object prototype
// member on lookup.
export type TagColorMap = Map<string, PaletteColorName>;

// Join GET /tag-values rows into a lookup map, dropping any row whose color isn't
// in the curated palette (defensive against payload skew, mirroring how the tag
// chips re-validate their shape before render).
export function buildTagColorMap(tagValues: Array<TagValue>): TagColorMap {
  const map: TagColorMap = new Map();
  for (const row of tagValues) {
    if (
      typeof row.key !== "string" ||
      typeof row.value !== "string" ||
      !isPaletteColorName(row.color)
    ) {
      continue;
    }
    map.set(tagColorKey(row.key, row.value), row.color);
  }
  return map;
}

// Resolve a tag's color from the map. Only grouped tags (non-null key) carry a
// color; standalone labels always return undefined.
export function tagColorFor(
  colors: TagColorMap | undefined,
  tagKey: string | null,
  value: string,
): PaletteColorName | undefined {
  if (colors === undefined || tagKey === null) {
    return undefined;
  }
  return colors.get(tagColorKey(tagKey, value));
}
