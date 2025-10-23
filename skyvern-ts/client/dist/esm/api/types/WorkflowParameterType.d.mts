export declare const WorkflowParameterType: {
    readonly String: "string";
    readonly Integer: "integer";
    readonly Float: "float";
    readonly Boolean: "boolean";
    readonly Json: "json";
    readonly FileUrl: "file_url";
    readonly CredentialId: "credential_id";
};
export type WorkflowParameterType = (typeof WorkflowParameterType)[keyof typeof WorkflowParameterType];
