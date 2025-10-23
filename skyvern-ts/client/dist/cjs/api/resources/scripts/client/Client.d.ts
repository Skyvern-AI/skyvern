import type { BaseClientOptions, BaseRequestOptions } from "../../../../BaseClient.js";
import * as core from "../../../../core/index.js";
export declare namespace Scripts {
    interface Options extends BaseClientOptions {
    }
    interface RequestOptions extends BaseRequestOptions {
    }
}
export declare class Scripts {
    protected readonly _options: Scripts.Options;
    constructor(_options?: Scripts.Options);
    /**
     * Run a script
     *
     * @param {string} scriptId - The unique identifier of the script
     * @param {Scripts.RequestOptions} requestOptions - Request-specific configuration.
     *
     * @throws {@link Skyvern.UnprocessableEntityError}
     *
     * @example
     *     await client.scripts.runScript("s_abc123")
     */
    runScript(scriptId: string, requestOptions?: Scripts.RequestOptions): core.HttpResponsePromise<unknown>;
    private __runScript;
    protected _getCustomAuthorizationHeaders(): Promise<Record<string, string | undefined>>;
}
