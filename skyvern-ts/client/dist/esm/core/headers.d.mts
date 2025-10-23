export declare function mergeHeaders<THeaderValue>(...headersArray: (Record<string, THeaderValue> | null | undefined)[]): Record<string, string | THeaderValue>;
export declare function mergeOnlyDefinedHeaders<THeaderValue>(...headersArray: (Record<string, THeaderValue> | null | undefined)[]): Record<string, THeaderValue>;
