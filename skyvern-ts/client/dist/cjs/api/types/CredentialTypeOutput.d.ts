/** Type of credential stored in the system. */
export declare const CredentialTypeOutput: {
    readonly Password: "password";
    readonly CreditCard: "credit_card";
};
export type CredentialTypeOutput = (typeof CredentialTypeOutput)[keyof typeof CredentialTypeOutput];
