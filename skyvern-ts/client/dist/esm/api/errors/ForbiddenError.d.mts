import type * as core from "../../core/index.mjs";
import * as errors from "../../errors/index.mjs";
export declare class ForbiddenError extends errors.SkyvernError {
    constructor(body?: unknown, rawResponse?: core.RawResponse);
}
