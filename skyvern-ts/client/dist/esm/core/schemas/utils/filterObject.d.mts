export declare function filterObject<T extends object, K extends keyof T>(obj: T, keysToInclude: K[]): Pick<T, K>;
