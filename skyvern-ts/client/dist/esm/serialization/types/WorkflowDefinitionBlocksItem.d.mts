import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
import { ActionBlock } from "./ActionBlock.mjs";
import { CodeBlock } from "./CodeBlock.mjs";
import { DownloadToS3Block } from "./DownloadToS3Block.mjs";
import { ExtractionBlock } from "./ExtractionBlock.mjs";
import { FileDownloadBlock } from "./FileDownloadBlock.mjs";
import { FileParserBlock } from "./FileParserBlock.mjs";
import { FileUploadBlock } from "./FileUploadBlock.mjs";
import { HttpRequestBlock } from "./HttpRequestBlock.mjs";
import { LoginBlock } from "./LoginBlock.mjs";
import { NavigationBlock } from "./NavigationBlock.mjs";
import { PdfParserBlock } from "./PdfParserBlock.mjs";
import { SendEmailBlock } from "./SendEmailBlock.mjs";
import { TaskBlock } from "./TaskBlock.mjs";
import { TaskV2Block } from "./TaskV2Block.mjs";
import { TextPromptBlock } from "./TextPromptBlock.mjs";
import { UploadToS3Block } from "./UploadToS3Block.mjs";
import { UrlBlock } from "./UrlBlock.mjs";
import { ValidationBlock } from "./ValidationBlock.mjs";
import { WaitBlock } from "./WaitBlock.mjs";
export declare const WorkflowDefinitionBlocksItem: core.serialization.Schema<serializers.WorkflowDefinitionBlocksItem.Raw, Skyvern.WorkflowDefinitionBlocksItem>;
export declare namespace WorkflowDefinitionBlocksItem {
    type Raw = WorkflowDefinitionBlocksItem.Action | WorkflowDefinitionBlocksItem.Code | WorkflowDefinitionBlocksItem.DownloadToS3 | WorkflowDefinitionBlocksItem.Extraction | WorkflowDefinitionBlocksItem.FileDownload | WorkflowDefinitionBlocksItem.FileUpload | WorkflowDefinitionBlocksItem.FileUrlParser | WorkflowDefinitionBlocksItem.ForLoop | WorkflowDefinitionBlocksItem.GotoUrl | WorkflowDefinitionBlocksItem.HttpRequest | WorkflowDefinitionBlocksItem.Login | WorkflowDefinitionBlocksItem.Navigation | WorkflowDefinitionBlocksItem.PdfParser | WorkflowDefinitionBlocksItem.SendEmail | WorkflowDefinitionBlocksItem.Task | WorkflowDefinitionBlocksItem.TaskV2 | WorkflowDefinitionBlocksItem.TextPrompt | WorkflowDefinitionBlocksItem.UploadToS3 | WorkflowDefinitionBlocksItem.Validation | WorkflowDefinitionBlocksItem.Wait;
    interface Action extends ActionBlock.Raw {
        block_type: "action";
    }
    interface Code extends CodeBlock.Raw {
        block_type: "code";
    }
    interface DownloadToS3 extends DownloadToS3Block.Raw {
        block_type: "download_to_s3";
    }
    interface Extraction extends ExtractionBlock.Raw {
        block_type: "extraction";
    }
    interface FileDownload extends FileDownloadBlock.Raw {
        block_type: "file_download";
    }
    interface FileUpload extends FileUploadBlock.Raw {
        block_type: "file_upload";
    }
    interface FileUrlParser extends FileParserBlock.Raw {
        block_type: "file_url_parser";
    }
    interface ForLoop extends serializers.ForLoopBlock.Raw {
        block_type: "for_loop";
    }
    interface GotoUrl extends UrlBlock.Raw {
        block_type: "goto_url";
    }
    interface HttpRequest extends HttpRequestBlock.Raw {
        block_type: "http_request";
    }
    interface Login extends LoginBlock.Raw {
        block_type: "login";
    }
    interface Navigation extends NavigationBlock.Raw {
        block_type: "navigation";
    }
    interface PdfParser extends PdfParserBlock.Raw {
        block_type: "pdf_parser";
    }
    interface SendEmail extends SendEmailBlock.Raw {
        block_type: "send_email";
    }
    interface Task extends TaskBlock.Raw {
        block_type: "task";
    }
    interface TaskV2 extends TaskV2Block.Raw {
        block_type: "task_v2";
    }
    interface TextPrompt extends TextPromptBlock.Raw {
        block_type: "text_prompt";
    }
    interface UploadToS3 extends UploadToS3Block.Raw {
        block_type: "upload_to_s3";
    }
    interface Validation extends ValidationBlock.Raw {
        block_type: "validation";
    }
    interface Wait extends WaitBlock.Raw {
        block_type: "wait";
    }
}
