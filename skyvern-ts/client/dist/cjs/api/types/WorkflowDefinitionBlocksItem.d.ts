import type * as Skyvern from "../index.js";
export type WorkflowDefinitionBlocksItem = Skyvern.WorkflowDefinitionBlocksItem.Action | Skyvern.WorkflowDefinitionBlocksItem.Code | Skyvern.WorkflowDefinitionBlocksItem.DownloadToS3 | Skyvern.WorkflowDefinitionBlocksItem.Extraction | Skyvern.WorkflowDefinitionBlocksItem.FileDownload | Skyvern.WorkflowDefinitionBlocksItem.FileUpload | Skyvern.WorkflowDefinitionBlocksItem.FileUrlParser | Skyvern.WorkflowDefinitionBlocksItem.ForLoop | Skyvern.WorkflowDefinitionBlocksItem.GotoUrl | Skyvern.WorkflowDefinitionBlocksItem.HttpRequest | Skyvern.WorkflowDefinitionBlocksItem.Login | Skyvern.WorkflowDefinitionBlocksItem.Navigation | Skyvern.WorkflowDefinitionBlocksItem.PdfParser | Skyvern.WorkflowDefinitionBlocksItem.SendEmail | Skyvern.WorkflowDefinitionBlocksItem.Task | Skyvern.WorkflowDefinitionBlocksItem.TaskV2 | Skyvern.WorkflowDefinitionBlocksItem.TextPrompt | Skyvern.WorkflowDefinitionBlocksItem.UploadToS3 | Skyvern.WorkflowDefinitionBlocksItem.Validation | Skyvern.WorkflowDefinitionBlocksItem.Wait;
export declare namespace WorkflowDefinitionBlocksItem {
    interface Action extends Skyvern.ActionBlock {
        block_type: "action";
    }
    interface Code extends Skyvern.CodeBlock {
        block_type: "code";
    }
    interface DownloadToS3 extends Skyvern.DownloadToS3Block {
        block_type: "download_to_s3";
    }
    interface Extraction extends Skyvern.ExtractionBlock {
        block_type: "extraction";
    }
    interface FileDownload extends Skyvern.FileDownloadBlock {
        block_type: "file_download";
    }
    interface FileUpload extends Skyvern.FileUploadBlock {
        block_type: "file_upload";
    }
    interface FileUrlParser extends Skyvern.FileParserBlock {
        block_type: "file_url_parser";
    }
    interface ForLoop extends Skyvern.ForLoopBlock {
        block_type: "for_loop";
    }
    interface GotoUrl extends Skyvern.UrlBlock {
        block_type: "goto_url";
    }
    interface HttpRequest extends Skyvern.HttpRequestBlock {
        block_type: "http_request";
    }
    interface Login extends Skyvern.LoginBlock {
        block_type: "login";
    }
    interface Navigation extends Skyvern.NavigationBlock {
        block_type: "navigation";
    }
    interface PdfParser extends Skyvern.PdfParserBlock {
        block_type: "pdf_parser";
    }
    interface SendEmail extends Skyvern.SendEmailBlock {
        block_type: "send_email";
    }
    interface Task extends Skyvern.TaskBlock {
        block_type: "task";
    }
    interface TaskV2 extends Skyvern.TaskV2Block {
        block_type: "task_v2";
    }
    interface TextPrompt extends Skyvern.TextPromptBlock {
        block_type: "text_prompt";
    }
    interface UploadToS3 extends Skyvern.UploadToS3Block {
        block_type: "upload_to_s3";
    }
    interface Validation extends Skyvern.ValidationBlock {
        block_type: "validation";
    }
    interface Wait extends Skyvern.WaitBlock {
        block_type: "wait";
    }
}
