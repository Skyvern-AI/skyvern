import type * as Skyvern from "../index.mjs";
export type ForLoopBlockYamlLoopBlocksItem = Skyvern.ForLoopBlockYamlLoopBlocksItem.Task | Skyvern.ForLoopBlockYamlLoopBlocksItem.ForLoop | Skyvern.ForLoopBlockYamlLoopBlocksItem.Code | Skyvern.ForLoopBlockYamlLoopBlocksItem.TextPrompt | Skyvern.ForLoopBlockYamlLoopBlocksItem.DownloadToS3 | Skyvern.ForLoopBlockYamlLoopBlocksItem.UploadToS3 | Skyvern.ForLoopBlockYamlLoopBlocksItem.FileUpload | Skyvern.ForLoopBlockYamlLoopBlocksItem.SendEmail | Skyvern.ForLoopBlockYamlLoopBlocksItem.FileUrlParser | Skyvern.ForLoopBlockYamlLoopBlocksItem.Validation | Skyvern.ForLoopBlockYamlLoopBlocksItem.Action | Skyvern.ForLoopBlockYamlLoopBlocksItem.Navigation | Skyvern.ForLoopBlockYamlLoopBlocksItem.Extraction | Skyvern.ForLoopBlockYamlLoopBlocksItem.Login | Skyvern.ForLoopBlockYamlLoopBlocksItem.Wait | Skyvern.ForLoopBlockYamlLoopBlocksItem.FileDownload | Skyvern.ForLoopBlockYamlLoopBlocksItem.GotoUrl | Skyvern.ForLoopBlockYamlLoopBlocksItem.PdfParser | Skyvern.ForLoopBlockYamlLoopBlocksItem.TaskV2 | Skyvern.ForLoopBlockYamlLoopBlocksItem.HttpRequest;
export declare namespace ForLoopBlockYamlLoopBlocksItem {
    interface Task extends Skyvern.TaskBlockYaml {
        block_type: "task";
    }
    interface ForLoop extends Skyvern.ForLoopBlockYaml {
        block_type: "for_loop";
    }
    interface Code extends Skyvern.CodeBlockYaml {
        block_type: "code";
    }
    interface TextPrompt extends Skyvern.TextPromptBlockYaml {
        block_type: "text_prompt";
    }
    interface DownloadToS3 extends Skyvern.DownloadToS3BlockYaml {
        block_type: "download_to_s3";
    }
    interface UploadToS3 extends Skyvern.UploadToS3BlockYaml {
        block_type: "upload_to_s3";
    }
    interface FileUpload extends Skyvern.FileUploadBlockYaml {
        block_type: "file_upload";
    }
    interface SendEmail extends Skyvern.SendEmailBlockYaml {
        block_type: "send_email";
    }
    interface FileUrlParser extends Skyvern.FileParserBlockYaml {
        block_type: "file_url_parser";
    }
    interface Validation extends Skyvern.ValidationBlockYaml {
        block_type: "validation";
    }
    interface Action extends Skyvern.ActionBlockYaml {
        block_type: "action";
    }
    interface Navigation extends Skyvern.NavigationBlockYaml {
        block_type: "navigation";
    }
    interface Extraction extends Skyvern.ExtractionBlockYaml {
        block_type: "extraction";
    }
    interface Login extends Skyvern.LoginBlockYaml {
        block_type: "login";
    }
    interface Wait extends Skyvern.WaitBlockYaml {
        block_type: "wait";
    }
    interface FileDownload extends Skyvern.FileDownloadBlockYaml {
        block_type: "file_download";
    }
    interface GotoUrl extends Skyvern.UrlBlockYaml {
        block_type: "goto_url";
    }
    interface PdfParser extends Skyvern.PdfParserBlockYaml {
        block_type: "pdf_parser";
    }
    interface TaskV2 extends Skyvern.TaskV2BlockYaml {
        block_type: "task_v2";
    }
    interface HttpRequest extends Skyvern.HttpRequestBlockYaml {
        block_type: "http_request";
    }
}
