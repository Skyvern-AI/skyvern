export type SecuritySchemeKey = string;
/**
 * A collection of security schemes, where the key is the name of the security scheme and the value is the list of scopes required for that scheme.
 * All schemes in the collection must be satisfied for authentication to be successful.
 */
export type SecuritySchemeCollection = Record<SecuritySchemeKey, AuthScope[]>;
export type AuthScope = string;
export type EndpointMetadata = {
    /**
     * An array of security scheme collections. Each collection represents an alternative way to authenticate.
     */
    security?: SecuritySchemeCollection[];
};
