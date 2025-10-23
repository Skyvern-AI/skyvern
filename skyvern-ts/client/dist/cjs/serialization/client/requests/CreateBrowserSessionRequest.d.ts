import type * as Skyvern from "../../../api/index.js";
import * as core from "../../../core/index.js";
import type * as serializers from "../../index.js";
import { ProxyLocation } from "../../types/ProxyLocation.js";
export declare const CreateBrowserSessionRequest: core.serialization.Schema<serializers.CreateBrowserSessionRequest.Raw, Skyvern.CreateBrowserSessionRequest>;
export declare namespace CreateBrowserSessionRequest {
    interface Raw {
        timeout?: number | null;
        proxy_location?: ProxyLocation.Raw | null;
    }
}
