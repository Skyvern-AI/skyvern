import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { BrowserSessionResponse } from "../types/BrowserSessionResponse.mjs";
export declare const Response: core.serialization.Schema<serializers.getBrowserSessions.Response.Raw, Skyvern.BrowserSessionResponse[]>;
export declare namespace Response {
    type Raw = BrowserSessionResponse.Raw[];
}
