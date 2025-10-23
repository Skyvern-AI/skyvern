import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
import { ContextParameterValue } from "./ContextParameterValue.mjs";
export declare const ContextParameter: core.serialization.ObjectSchema<serializers.ContextParameter.Raw, Skyvern.ContextParameter>;
export declare namespace ContextParameter {
    interface Raw {
        key: string;
        description?: string | null;
        source: serializers.ContextParameterSource.Raw;
        value?: ContextParameterValue.Raw | null;
    }
}
