import { GeoTarget } from "@/api/types";
import {
  COUNTRY_FLAGS,
  COUNTRY_NAMES,
  SUPPORTED_COUNTRY_CODES,
  SupportedCountryCode,
} from "./geoData";

export type SearchResultItem = {
  type: "country" | "subdivision" | "city";
  label: string;
  value: GeoTarget;
  description?: string;
  icon?: string;
};

export type GroupedSearchResults = {
  countries: SearchResultItem[];
  subdivisions: SearchResultItem[];
  cities: SearchResultItem[];
};

let cscModule: typeof import("country-state-city") | null = null;

async function loadCsc() {
  if (!cscModule) {
    cscModule = await import("country-state-city");
  }
  return cscModule;
}

export async function searchGeoData(
  query: string,
): Promise<GroupedSearchResults> {
  const normalizedQuery = query.trim().toLowerCase();
  const queryMatchesISP = normalizedQuery.includes("isp");
  const results: GroupedSearchResults = {
    countries: [],
    subdivisions: [],
    cities: [],
  };

  // 1. Countries (Always search supported countries)
  // We can do this without loading the heavy lib if we use our hardcoded lists
  SUPPORTED_COUNTRY_CODES.forEach((code) => {
    const name = COUNTRY_NAMES[code];
    const matchesCountry =
      name.toLowerCase().includes(normalizedQuery) ||
      code.toLowerCase().includes(normalizedQuery);
    const shouldIncludeUSForISP = code === "US" && queryMatchesISP;
    if (matchesCountry || shouldIncludeUSForISP) {
      results.countries.push({
        type: "country",
        label: name,
        value: { country: code },
        description: code,
        icon: COUNTRY_FLAGS[code],
      });
    }
  });

  if (results.countries.length > 0) {
    const usIndex = results.countries.findIndex(
      (item) => item.value.country === "US" && !item.value.isISP,
    );
    if (usIndex !== -1 || queryMatchesISP) {
      const ispItem: SearchResultItem = {
        type: "country",
        label: "United States (ISP)",
        value: { country: "US", isISP: true },
        description: "US",
        icon: COUNTRY_FLAGS.US,
      };
      const insertIndex = usIndex !== -1 ? usIndex + 1 : 0;
      results.countries.splice(insertIndex, 0, ispItem);
    }
  }

  // If query is very short, just return countries to save perf
  if (normalizedQuery.length < 2) {
    return results;
  }

  // 2. Subdivisions & Cities (Load heavy lib)
  const csc = await loadCsc();

  // Search Subdivisions
  // We only search subdivisions of SUPPORTED countries
  for (const countryCode of SUPPORTED_COUNTRY_CODES) {
    const states = csc.State.getStatesOfCountry(countryCode);
    for (const state of states) {
      if (
        state.name.toLowerCase().includes(normalizedQuery) ||
        state.isoCode.toLowerCase() === normalizedQuery
      ) {
        results.subdivisions.push({
          type: "subdivision",
          label: state.name,
          value: { country: countryCode, subdivision: state.isoCode },
          description: `${state.isoCode}, ${COUNTRY_NAMES[countryCode]}`,
          icon: COUNTRY_FLAGS[countryCode],
        });
      }

      // Limit subdivisions per country to avoid overwhelming
      if (results.subdivisions.length >= 10) break;
    }
  }

  // Search Cities
  // Searching ALL cities of ALL supported countries is heavy.
  // We optimize by breaking early once we have enough results.
  const prefixMatches: SearchResultItem[] = [];
  const partialMatches: SearchResultItem[] = [];

  for (const countryCode of SUPPORTED_COUNTRY_CODES) {
    const cities = csc.City.getCitiesOfCountry(countryCode) || [];

    for (const city of cities) {
      const nameLower = city.name.toLowerCase();
      const item: SearchResultItem = {
        type: "city",
        label: city.name,
        value: {
          country: countryCode,
          subdivision: city.stateCode,
          city: city.name,
        },
        description: `${city.stateCode}, ${COUNTRY_NAMES[countryCode]}`,
        icon: COUNTRY_FLAGS[countryCode],
      };

      if (nameLower === normalizedQuery) {
        // Exact match goes to the front
        prefixMatches.unshift(item);
      } else if (nameLower.startsWith(normalizedQuery)) {
        prefixMatches.push(item);
      } else if (nameLower.includes(normalizedQuery)) {
        partialMatches.push(item);
      }

      // Break if we have enough total cities
      if (prefixMatches.length + partialMatches.length > 100) break;
    }
    if (prefixMatches.length + partialMatches.length > 100) break;
  }

  results.cities = [...prefixMatches, ...partialMatches];

  // Slice to final limits
  results.subdivisions = results.subdivisions.slice(0, 5);
  results.cities = results.cities.slice(0, 20);

  return results;
}

export async function getCountryName(code: string): Promise<string> {
  if (code in COUNTRY_NAMES) {
    return COUNTRY_NAMES[code as SupportedCountryCode];
  }
  const csc = await loadCsc();
  return csc.Country.getCountryByCode(code)?.name || code;
}

export async function getSubdivisionName(
  countryCode: string,
  subdivisionCode: string,
): Promise<string> {
  const csc = await loadCsc();
  return (
    csc.State.getStateByCodeAndCountry(subdivisionCode, countryCode)?.name ||
    subdivisionCode
  );
}
