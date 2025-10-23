export declare const FileStorageType: {
    readonly S3: "s3";
    readonly Azure: "azure";
};
export type FileStorageType = (typeof FileStorageType)[keyof typeof FileStorageType];
