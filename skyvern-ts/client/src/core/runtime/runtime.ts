interface DenoGlobal {
    version: {
        deno: string;
    };
}

interface BunGlobal {
    version: string;
}

declare const Deno: DenoGlobal | undefined;
declare const Bun: BunGlobal | undefined;
declare const EdgeRuntime: string | undefined;
declare const self: typeof globalThis.self & {
    importScripts?: unknown;
};

/**
 * A constant that indicates which environment and version the SDK is running in.
 */
export const RUNTIME: Runtime = evaluateRuntime();

export interface Runtime {
    type: "browser" | "web-worker" | "deno" | "bun" | "node" | "react-native" | "unknown" | "workerd" | "edge-runtime";
    version?: string;
    parsedVersion?: number;
}

function evaluateRuntime(): Runtime {
    /**
     * A constant that indicates whether the environment the code is running is a Web Browser.
     */
    const isBrowser = typeof window !== "undefined" && typeof window.document !== "undefined";
    if (isBrowser) {
        return {
            type: "browser",
            version: window.navigator.userAgent,
        };
    }

    /**
     * A constant that indicates whether the environment the code is running is Cloudflare.
     * https://developers.cloudflare.com/workers/runtime-apis/web-standards/#navigatoruseragent
     */
    const isCloudflare = typeof globalThis !== "undefined" && globalThis?.navigator?.userAgent === "Cloudflare-Workers";
    if (isCloudflare) {
        return {
            type: "workerd",
        };
    }

    /**
     * A constant that indicates whether the environment the code is running is Edge Runtime.
     * https://vercel.com/docs/functions/runtimes/edge-runtime#check-if-you're-running-on-the-edge-runtime
     */
    const isEdgeRuntime = typeof EdgeRuntime === "string";
    if (isEdgeRuntime) {
        return {
            type: "edge-runtime",
        };
    }

    /**
     * A constant that indicates whether the environment the code is running is a Web Worker.
     */
    const isWebWorker =
        typeof self === "object" &&
        typeof self?.importScripts === "function" &&
        (self.constructor?.name === "DedicatedWorkerGlobalScope" ||
            self.constructor?.name === "ServiceWorkerGlobalScope" ||
            self.constructor?.name === "SharedWorkerGlobalScope");
    if (isWebWorker) {
        return {
            type: "web-worker",
        };
    }

    /**
     * A constant that indicates whether the environment the code is running is Deno.
     * FYI Deno spoofs process.versions.node, see https://deno.land/std@0.177.0/node/process.ts?s=versions
     */
    const isDeno =
        typeof Deno !== "undefined" && typeof Deno.version !== "undefined" && typeof Deno.version.deno !== "undefined";
    if (isDeno) {
        return {
            type: "deno",
            version: Deno.version.deno,
        };
    }

    /**
     * A constant that indicates whether the environment the code is running is Bun.sh.
     */
    const isBun = typeof Bun !== "undefined" && typeof Bun.version !== "undefined";
    if (isBun) {
        return {
            type: "bun",
            version: Bun.version,
        };
    }

    /**
     * A constant that indicates whether the environment the code is running is Node.JS.
     */
    const isNode =
        typeof process !== "undefined" &&
        "version" in process &&
        !!process.version &&
        "versions" in process &&
        !!process.versions?.node;
    if (isNode) {
        return {
            type: "node",
            version: process.versions.node,
            parsedVersion: Number(process.versions.node.split(".")[0]),
        };
    }

    /**
     * A constant that indicates whether the environment the code is running is in React-Native.
     * https://github.com/facebook/react-native/blob/main/packages/react-native/Libraries/Core/setUpNavigator.js
     */
    const isReactNative = typeof navigator !== "undefined" && navigator?.product === "ReactNative";
    if (isReactNative) {
        return {
            type: "react-native",
        };
    }

    return {
        type: "unknown",
    };
}
