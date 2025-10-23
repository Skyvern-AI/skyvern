/** Type of 2FA/TOTP method used. */
export declare const TotpType: {
    readonly Authenticator: "authenticator";
    readonly Email: "email";
    readonly Text: "text";
    readonly None: "none";
};
export type TotpType = (typeof TotpType)[keyof typeof TotpType];
