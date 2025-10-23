import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
import { ContextParameterValue } from "./ContextParameterValue.js";
export declare const ContextParameter: core.serialization.ObjectSchema<serializers.ContextParameter.Raw, Skyvern.ContextParameter>;
export declare namespace ContextParameter {
    interface Raw {
        key: string;
        description?: string | null;
        source: serializers.ContextParameterSource.Raw;
        value?: ContextParameterValue.Raw | null;
    }
}
