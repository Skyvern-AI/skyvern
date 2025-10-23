import type * as Skyvern from "../index.mjs";
/**
 * Response model for credential operations.
 */
export interface CredentialResponse {
    /** Unique identifier for the credential */
    credential_id: string;
    /** The credential data */
    credential: CredentialResponse.Credential;
    /** Type of the credential */
    credential_type: Skyvern.CredentialTypeOutput;
    /** Name of the credential */
    name: string;
}
export declare namespace CredentialResponse {
    /**
     * The credential data
     */
    type Credential = Skyvern.PasswordCredentialResponse | Skyvern.CreditCardCredentialResponse;
}
