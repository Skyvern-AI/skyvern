export declare const RunEngine: {
    readonly Skyvern10: "skyvern-1.0";
    readonly Skyvern20: "skyvern-2.0";
    readonly OpenaiCua: "openai-cua";
    readonly AnthropicCua: "anthropic-cua";
    readonly UiTars: "ui-tars";
};
export type RunEngine = (typeof RunEngine)[keyof typeof RunEngine];
