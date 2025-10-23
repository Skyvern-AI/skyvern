import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const CredentialTypeOutput: core.serialization.Schema<serializers.CredentialTypeOutput.Raw, Skyvern.CredentialTypeOutput>;
export declare namespace CredentialTypeOutput {
    type Raw = "password" | "credit_card";
}
