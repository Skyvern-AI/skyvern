export interface DownloadToS3BlockYaml {
    label: string;
    continue_on_failure?: boolean;
    model?: Record<string, unknown>;
    url: string;
}
