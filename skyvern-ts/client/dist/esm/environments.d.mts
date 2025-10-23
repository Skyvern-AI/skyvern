export declare const SkyvernEnvironment: {
    readonly Production: "https://api.skyvern.com";
    readonly Staging: "https://api-staging.skyvern.com";
    readonly Development: "http://localhost:8000";
};
export type SkyvernEnvironment = typeof SkyvernEnvironment.Production | typeof SkyvernEnvironment.Staging | typeof SkyvernEnvironment.Development;
