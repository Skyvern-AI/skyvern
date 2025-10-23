export interface UploadToS3BlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    path?: string;
}
