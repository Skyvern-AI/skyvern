export declare const OtpType: {
    readonly Totp: "totp";
    readonly MagicLink: "magic_link";
};
export type OtpType = (typeof OtpType)[keyof typeof OtpType];
