/**
 * A constant that indicates which environment and version the SDK is running in.
 */
export declare const RUNTIME: Runtime;
export interface Runtime {
    type: "browser" | "web-worker" | "deno" | "bun" | "node" | "react-native" | "unknown" | "workerd" | "edge-runtime";
    version?: string;
    parsedVersion?: number;
}
