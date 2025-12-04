import { GeoTarget, ProxyLocation } from "@/api/types";

export const SUPPORTED_COUNTRY_CODES = [
  "US",
  "AR",
  "AU",
  "BR",
  "CA",
  "DE",
  "ES",
  "FR",
  "GB",
  "IE",
  "IN",
  "IT",
  "JP",
  "MX",
  "NL",
  "NZ",
  "TR",
  "ZA",
] as const;

export type SupportedCountryCode = (typeof SUPPORTED_COUNTRY_CODES)[number];

export const COUNTRY_NAMES: Record<SupportedCountryCode, string> = {
  US: "United States",
  AR: "Argentina",
  AU: "Australia",
  BR: "Brazil",
  CA: "Canada",
  DE: "Germany",
  ES: "Spain",
  FR: "France",
  GB: "United Kingdom",
  IE: "Ireland",
  IN: "India",
  IT: "Italy",
  JP: "Japan",
  MX: "Mexico",
  NL: "Netherlands",
  NZ: "New Zealand",
  TR: "Turkey",
  ZA: "South Africa",
};

export const COUNTRY_FLAGS: Record<SupportedCountryCode, string> = {
  US: "🇺🇸",
  AR: "🇦🇷",
  AU: "🇦🇺",
  BR: "🇧🇷",
  CA: "🇨🇦",
  DE: "🇩🇪",
  ES: "🇪🇸",
  FR: "🇫🇷",
  GB: "🇬🇧",
  IE: "🇮🇪",
  IN: "🇮🇳",
  IT: "🇮🇹",
  JP: "🇯🇵",
  MX: "🇲🇽",
  NL: "🇳🇱",
  NZ: "🇳🇿",
  TR: "🇹🇷",
  ZA: "🇿🇦",
};

// Map legacy ProxyLocation to Country Code
const PROXY_LOCATION_TO_COUNTRY: Record<string, string> = {
  [ProxyLocation.Residential]: "US",
  [ProxyLocation.ResidentialISP]: "US",
  [ProxyLocation.ResidentialAR]: "AR",
  [ProxyLocation.ResidentialAU]: "AU",
  [ProxyLocation.ResidentialBR]: "BR",
  [ProxyLocation.ResidentialCA]: "CA",
  [ProxyLocation.ResidentialDE]: "DE",
  [ProxyLocation.ResidentialES]: "ES",
  [ProxyLocation.ResidentialFR]: "FR",
  [ProxyLocation.ResidentialGB]: "GB",
  [ProxyLocation.ResidentialIE]: "IE",
  [ProxyLocation.ResidentialIN]: "IN",
  [ProxyLocation.ResidentialIT]: "IT",
  [ProxyLocation.ResidentialJP]: "JP",
  [ProxyLocation.ResidentialMX]: "MX",
  [ProxyLocation.ResidentialNL]: "NL",
  [ProxyLocation.ResidentialNZ]: "NZ",
  [ProxyLocation.ResidentialTR]: "TR",
  [ProxyLocation.ResidentialZA]: "ZA",
};

// Reverse map for round-tripping simple country selections
const COUNTRY_TO_PROXY_LOCATION: Record<string, ProxyLocation> = {
  US: ProxyLocation.Residential,
  AR: ProxyLocation.ResidentialAR,
  AU: ProxyLocation.ResidentialAU,
  BR: ProxyLocation.ResidentialBR,
  CA: ProxyLocation.ResidentialCA,
  DE: ProxyLocation.ResidentialDE,
  ES: ProxyLocation.ResidentialES,
  FR: ProxyLocation.ResidentialFR,
  GB: ProxyLocation.ResidentialGB,
  IE: ProxyLocation.ResidentialIE,
  IN: ProxyLocation.ResidentialIN,
  IT: ProxyLocation.ResidentialIT,
  JP: ProxyLocation.ResidentialJP,
  MX: ProxyLocation.ResidentialMX,
  NL: ProxyLocation.ResidentialNL,
  NZ: ProxyLocation.ResidentialNZ,
  TR: ProxyLocation.ResidentialTR,
  ZA: ProxyLocation.ResidentialZA,
};

export function proxyLocationToGeoTarget(
  input: ProxyLocation,
): GeoTarget | null {
  if (!input) return null;

  // If it's already a GeoTarget object
  if (typeof input === "object" && "country" in input) {
    return input;
  }

  // If it's a legacy string
  if (typeof input === "string") {
    if (input === ProxyLocation.None) return null;
    if (input === ProxyLocation.ResidentialISP) {
      return { country: "US", isISP: true };
    }
    const country = PROXY_LOCATION_TO_COUNTRY[input];
    if (country) {
      return { country };
    }
  }

  return null;
}

export function geoTargetToProxyLocationInput(
  target: GeoTarget | null,
): ProxyLocation {
  if (!target) return ProxyLocation.None;

  if (target.isISP) {
    return ProxyLocation.ResidentialISP;
  }

  // Try to map back to legacy enum if it's just a country
  if (target.country && !target.subdivision && !target.city) {
    const legacyLocation = COUNTRY_TO_PROXY_LOCATION[target.country];
    if (legacyLocation) {
      return legacyLocation;
    }
  }

  // Otherwise return the object
  return target;
}

export function formatGeoTarget(target: GeoTarget | null): string {
  if (!target || !target.country) return "No Proxy";

  const parts = [];

  // Add Flag
  if (target.country in COUNTRY_FLAGS) {
    parts.push(COUNTRY_FLAGS[target.country as SupportedCountryCode]);
  }

  if (target.city) parts.push(target.city);
  if (target.subdivision) parts.push(target.subdivision);

  // Country Name
  const countryName =
    COUNTRY_NAMES[target.country as SupportedCountryCode] || target.country;
  if (target.isISP) {
    parts.push(`${countryName} (ISP)`);
  } else {
    parts.push(countryName);
  }

  return parts.join(" ");
}

export function formatGeoTargetCompact(target: GeoTarget | null): string {
  if (!target || !target.country) return "No Proxy";

  const parts = [];
  if (target.city) parts.push(target.city);
  if (target.subdivision) parts.push(target.subdivision);
  const countryName =
    COUNTRY_NAMES[target.country as SupportedCountryCode] || target.country;
  if (target.isISP) {
    parts.push(`${countryName} (ISP)`);
  } else {
    parts.push(countryName);
  }

  const text = parts.join(", ");
  const flag = COUNTRY_FLAGS[target.country as SupportedCountryCode] || "";

  return `${flag} ${text}`.trim();
}
