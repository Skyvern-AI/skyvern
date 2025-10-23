export declare const FileType: {
    readonly Csv: "csv";
    readonly Excel: "excel";
    readonly Pdf: "pdf";
};
export type FileType = (typeof FileType)[keyof typeof FileType];
