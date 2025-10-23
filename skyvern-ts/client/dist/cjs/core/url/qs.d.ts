interface QueryStringOptions {
    arrayFormat?: "indices" | "repeat";
    encode?: boolean;
}
export declare function toQueryString(obj: unknown, options?: QueryStringOptions): string;
export {};
