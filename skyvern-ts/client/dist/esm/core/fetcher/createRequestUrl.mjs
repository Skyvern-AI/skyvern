import { toQueryString } from "../url/qs.mjs";
export function createRequestUrl(baseUrl, queryParameters) {
    const queryString = toQueryString(queryParameters, { arrayFormat: "repeat" });
    return queryString ? `${baseUrl}?${queryString}` : baseUrl;
}
