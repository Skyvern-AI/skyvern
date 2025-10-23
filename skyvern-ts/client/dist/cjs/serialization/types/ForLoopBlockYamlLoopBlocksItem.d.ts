import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
import { ActionBlockYaml } from "./ActionBlockYaml.js";
import { CodeBlockYaml } from "./CodeBlockYaml.js";
import { DownloadToS3BlockYaml } from "./DownloadToS3BlockYaml.js";
import { ExtractionBlockYaml } from "./ExtractionBlockYaml.js";
import { FileDownloadBlockYaml } from "./FileDownloadBlockYaml.js";
import { FileParserBlockYaml } from "./FileParserBlockYaml.js";
import { FileUploadBlockYaml } from "./FileUploadBlockYaml.js";
import { HttpRequestBlockYaml } from "./HttpRequestBlockYaml.js";
import { LoginBlockYaml } from "./LoginBlockYaml.js";
import { NavigationBlockYaml } from "./NavigationBlockYaml.js";
import { PdfParserBlockYaml } from "./PdfParserBlockYaml.js";
import { SendEmailBlockYaml } from "./SendEmailBlockYaml.js";
import { TaskBlockYaml } from "./TaskBlockYaml.js";
import { TaskV2BlockYaml } from "./TaskV2BlockYaml.js";
import { TextPromptBlockYaml } from "./TextPromptBlockYaml.js";
import { UploadToS3BlockYaml } from "./UploadToS3BlockYaml.js";
import { UrlBlockYaml } from "./UrlBlockYaml.js";
import { ValidationBlockYaml } from "./ValidationBlockYaml.js";
import { WaitBlockYaml } from "./WaitBlockYaml.js";
export declare const ForLoopBlockYamlLoopBlocksItem: core.serialization.Schema<serializers.ForLoopBlockYamlLoopBlocksItem.Raw, Skyvern.ForLoopBlockYamlLoopBlocksItem>;
export declare namespace ForLoopBlockYamlLoopBlocksItem {
    type Raw = ForLoopBlockYamlLoopBlocksItem.Task | ForLoopBlockYamlLoopBlocksItem.ForLoop | ForLoopBlockYamlLoopBlocksItem.Code | ForLoopBlockYamlLoopBlocksItem.TextPrompt | ForLoopBlockYamlLoopBlocksItem.DownloadToS3 | ForLoopBlockYamlLoopBlocksItem.UploadToS3 | ForLoopBlockYamlLoopBlocksItem.FileUpload | ForLoopBlockYamlLoopBlocksItem.SendEmail | ForLoopBlockYamlLoopBlocksItem.FileUrlParser | ForLoopBlockYamlLoopBlocksItem.Validation | ForLoopBlockYamlLoopBlocksItem.Action | ForLoopBlockYamlLoopBlocksItem.Navigation | ForLoopBlockYamlLoopBlocksItem.Extraction | ForLoopBlockYamlLoopBlocksItem.Login | ForLoopBlockYamlLoopBlocksItem.Wait | ForLoopBlockYamlLoopBlocksItem.FileDownload | ForLoopBlockYamlLoopBlocksItem.GotoUrl | ForLoopBlockYamlLoopBlocksItem.PdfParser | ForLoopBlockYamlLoopBlocksItem.TaskV2 | ForLoopBlockYamlLoopBlocksItem.HttpRequest;
    interface Task extends TaskBlockYaml.Raw {
        block_type: "task";
    }
    interface ForLoop extends serializers.ForLoopBlockYaml.Raw {
        block_type: "for_loop";
    }
    interface Code extends CodeBlockYaml.Raw {
        block_type: "code";
    }
    interface TextPrompt extends TextPromptBlockYaml.Raw {
        block_type: "text_prompt";
    }
    interface DownloadToS3 extends DownloadToS3BlockYaml.Raw {
        block_type: "download_to_s3";
    }
    interface UploadToS3 extends UploadToS3BlockYaml.Raw {
        block_type: "upload_to_s3";
    }
    interface FileUpload extends FileUploadBlockYaml.Raw {
        block_type: "file_upload";
    }
    interface SendEmail extends SendEmailBlockYaml.Raw {
        block_type: "send_email";
    }
    interface FileUrlParser extends FileParserBlockYaml.Raw {
        block_type: "file_url_parser";
    }
    interface Validation extends ValidationBlockYaml.Raw {
        block_type: "validation";
    }
    interface Action extends ActionBlockYaml.Raw {
        block_type: "action";
    }
    interface Navigation extends NavigationBlockYaml.Raw {
        block_type: "navigation";
    }
    interface Extraction extends ExtractionBlockYaml.Raw {
        block_type: "extraction";
    }
    interface Login extends LoginBlockYaml.Raw {
        block_type: "login";
    }
    interface Wait extends WaitBlockYaml.Raw {
        block_type: "wait";
    }
    interface FileDownload extends FileDownloadBlockYaml.Raw {
        block_type: "file_download";
    }
    interface GotoUrl extends UrlBlockYaml.Raw {
        block_type: "goto_url";
    }
    interface PdfParser extends PdfParserBlockYaml.Raw {
        block_type: "pdf_parser";
    }
    interface TaskV2 extends TaskV2BlockYaml.Raw {
        block_type: "task_v2";
    }
    interface HttpRequest extends HttpRequestBlockYaml.Raw {
        block_type: "http_request";
    }
}
