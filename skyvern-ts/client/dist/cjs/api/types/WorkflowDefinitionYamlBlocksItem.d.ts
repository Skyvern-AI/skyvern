import type * as Skyvern from "../index.js";
export type WorkflowDefinitionYamlBlocksItem = Skyvern.WorkflowDefinitionYamlBlocksItem.Action | Skyvern.WorkflowDefinitionYamlBlocksItem.Code | Skyvern.WorkflowDefinitionYamlBlocksItem.DownloadToS3 | Skyvern.WorkflowDefinitionYamlBlocksItem.Extraction | Skyvern.WorkflowDefinitionYamlBlocksItem.FileDownload | Skyvern.WorkflowDefinitionYamlBlocksItem.FileUpload | Skyvern.WorkflowDefinitionYamlBlocksItem.FileUrlParser | Skyvern.WorkflowDefinitionYamlBlocksItem.ForLoop | Skyvern.WorkflowDefinitionYamlBlocksItem.GotoUrl | Skyvern.WorkflowDefinitionYamlBlocksItem.HttpRequest | Skyvern.WorkflowDefinitionYamlBlocksItem.Login | Skyvern.WorkflowDefinitionYamlBlocksItem.Navigation | Skyvern.WorkflowDefinitionYamlBlocksItem.PdfParser | Skyvern.WorkflowDefinitionYamlBlocksItem.SendEmail | Skyvern.WorkflowDefinitionYamlBlocksItem.Task | Skyvern.WorkflowDefinitionYamlBlocksItem.TaskV2 | Skyvern.WorkflowDefinitionYamlBlocksItem.TextPrompt | Skyvern.WorkflowDefinitionYamlBlocksItem.UploadToS3 | Skyvern.WorkflowDefinitionYamlBlocksItem.Validation | Skyvern.WorkflowDefinitionYamlBlocksItem.Wait;
export declare namespace WorkflowDefinitionYamlBlocksItem {
    interface Action extends Skyvern.ActionBlockYaml {
        block_type: "action";
    }
    interface Code extends Skyvern.CodeBlockYaml {
        block_type: "code";
    }
    interface DownloadToS3 extends Skyvern.DownloadToS3BlockYaml {
        block_type: "download_to_s3";
    }
    interface Extraction extends Skyvern.ExtractionBlockYaml {
        block_type: "extraction";
    }
    interface FileDownload extends Skyvern.FileDownloadBlockYaml {
        block_type: "file_download";
    }
    interface FileUpload extends Skyvern.FileUploadBlockYaml {
        block_type: "file_upload";
    }
    interface FileUrlParser extends Skyvern.FileParserBlockYaml {
        block_type: "file_url_parser";
    }
    interface ForLoop extends Skyvern.ForLoopBlockYaml {
        block_type: "for_loop";
    }
    interface GotoUrl extends Skyvern.UrlBlockYaml {
        block_type: "goto_url";
    }
    interface HttpRequest extends Skyvern.HttpRequestBlockYaml {
        block_type: "http_request";
    }
    interface Login extends Skyvern.LoginBlockYaml {
        block_type: "login";
    }
    interface Navigation extends Skyvern.NavigationBlockYaml {
        block_type: "navigation";
    }
    interface PdfParser extends Skyvern.PdfParserBlockYaml {
        block_type: "pdf_parser";
    }
    interface SendEmail extends Skyvern.SendEmailBlockYaml {
        block_type: "send_email";
    }
    interface Task extends Skyvern.TaskBlockYaml {
        block_type: "task";
    }
    interface TaskV2 extends Skyvern.TaskV2BlockYaml {
        block_type: "task_v2";
    }
    interface TextPrompt extends Skyvern.TextPromptBlockYaml {
        block_type: "text_prompt";
    }
    interface UploadToS3 extends Skyvern.UploadToS3BlockYaml {
        block_type: "upload_to_s3";
    }
    interface Validation extends Skyvern.ValidationBlockYaml {
        block_type: "validation";
    }
    interface Wait extends Skyvern.WaitBlockYaml {
        block_type: "wait";
    }
}
