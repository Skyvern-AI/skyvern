import type * as core from "../../core/index.js";
import * as errors from "../../errors/index.js";
export declare class BadRequestError extends errors.SkyvernError {
    constructor(body?: unknown, rawResponse?: core.RawResponse);
}
