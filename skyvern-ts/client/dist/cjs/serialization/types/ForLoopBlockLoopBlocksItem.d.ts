import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
import { ActionBlock } from "./ActionBlock.js";
import { CodeBlock } from "./CodeBlock.js";
import { DownloadToS3Block } from "./DownloadToS3Block.js";
import { ExtractionBlock } from "./ExtractionBlock.js";
import { FileDownloadBlock } from "./FileDownloadBlock.js";
import { FileParserBlock } from "./FileParserBlock.js";
import { FileUploadBlock } from "./FileUploadBlock.js";
import { HttpRequestBlock } from "./HttpRequestBlock.js";
import { LoginBlock } from "./LoginBlock.js";
import { NavigationBlock } from "./NavigationBlock.js";
import { PdfParserBlock } from "./PdfParserBlock.js";
import { SendEmailBlock } from "./SendEmailBlock.js";
import { TaskBlock } from "./TaskBlock.js";
import { TaskV2Block } from "./TaskV2Block.js";
import { TextPromptBlock } from "./TextPromptBlock.js";
import { UploadToS3Block } from "./UploadToS3Block.js";
import { UrlBlock } from "./UrlBlock.js";
import { ValidationBlock } from "./ValidationBlock.js";
import { WaitBlock } from "./WaitBlock.js";
export declare const ForLoopBlockLoopBlocksItem: core.serialization.Schema<serializers.ForLoopBlockLoopBlocksItem.Raw, Skyvern.ForLoopBlockLoopBlocksItem>;
export declare namespace ForLoopBlockLoopBlocksItem {
    type Raw = ForLoopBlockLoopBlocksItem.Action | ForLoopBlockLoopBlocksItem.Code | ForLoopBlockLoopBlocksItem.DownloadToS3 | ForLoopBlockLoopBlocksItem.Extraction | ForLoopBlockLoopBlocksItem.FileDownload | ForLoopBlockLoopBlocksItem.FileUpload | ForLoopBlockLoopBlocksItem.FileUrlParser | ForLoopBlockLoopBlocksItem.ForLoop | ForLoopBlockLoopBlocksItem.GotoUrl | ForLoopBlockLoopBlocksItem.HttpRequest | ForLoopBlockLoopBlocksItem.Login | ForLoopBlockLoopBlocksItem.Navigation | ForLoopBlockLoopBlocksItem.PdfParser | ForLoopBlockLoopBlocksItem.SendEmail | ForLoopBlockLoopBlocksItem.Task | ForLoopBlockLoopBlocksItem.TaskV2 | ForLoopBlockLoopBlocksItem.TextPrompt | ForLoopBlockLoopBlocksItem.UploadToS3 | ForLoopBlockLoopBlocksItem.Validation | ForLoopBlockLoopBlocksItem.Wait;
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
