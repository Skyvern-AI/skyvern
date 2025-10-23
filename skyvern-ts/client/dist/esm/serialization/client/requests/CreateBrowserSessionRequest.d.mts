import type * as Skyvern from "../../../api/index.mjs";
import * as core from "../../../core/index.mjs";
import type * as serializers from "../../index.mjs";
import { ProxyLocation } from "../../types/ProxyLocation.mjs";
export declare const CreateBrowserSessionRequest: core.serialization.Schema<serializers.CreateBrowserSessionRequest.Raw, Skyvern.CreateBrowserSessionRequest>;
export declare namespace CreateBrowserSessionRequest {
    interface Raw {
        timeout?: number | null;
        proxy_location?: ProxyLocation.Raw | null;
    }
}
