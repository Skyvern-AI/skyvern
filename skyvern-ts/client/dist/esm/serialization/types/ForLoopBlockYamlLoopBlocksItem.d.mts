import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
import { ActionBlockYaml } from "./ActionBlockYaml.mjs";
import { CodeBlockYaml } from "./CodeBlockYaml.mjs";
import { DownloadToS3BlockYaml } from "./DownloadToS3BlockYaml.mjs";
import { ExtractionBlockYaml } from "./ExtractionBlockYaml.mjs";
import { FileDownloadBlockYaml } from "./FileDownloadBlockYaml.mjs";
import { FileParserBlockYaml } from "./FileParserBlockYaml.mjs";
import { FileUploadBlockYaml } from "./FileUploadBlockYaml.mjs";
import { HttpRequestBlockYaml } from "./HttpRequestBlockYaml.mjs";
import { LoginBlockYaml } from "./LoginBlockYaml.mjs";
import { NavigationBlockYaml } from "./NavigationBlockYaml.mjs";
import { PdfParserBlockYaml } from "./PdfParserBlockYaml.mjs";
import { SendEmailBlockYaml } from "./SendEmailBlockYaml.mjs";
import { TaskBlockYaml } from "./TaskBlockYaml.mjs";
import { TaskV2BlockYaml } from "./TaskV2BlockYaml.mjs";
import { TextPromptBlockYaml } from "./TextPromptBlockYaml.mjs";
import { UploadToS3BlockYaml } from "./UploadToS3BlockYaml.mjs";
import { UrlBlockYaml } from "./UrlBlockYaml.mjs";
import { ValidationBlockYaml } from "./ValidationBlockYaml.mjs";
import { WaitBlockYaml } from "./WaitBlockYaml.mjs";
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
