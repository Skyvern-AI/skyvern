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
export declare const WorkflowDefinitionYamlBlocksItem: core.serialization.Schema<serializers.WorkflowDefinitionYamlBlocksItem.Raw, Skyvern.WorkflowDefinitionYamlBlocksItem>;
export declare namespace WorkflowDefinitionYamlBlocksItem {
    type Raw = WorkflowDefinitionYamlBlocksItem.Action | WorkflowDefinitionYamlBlocksItem.Code | WorkflowDefinitionYamlBlocksItem.DownloadToS3 | WorkflowDefinitionYamlBlocksItem.Extraction | WorkflowDefinitionYamlBlocksItem.FileDownload | WorkflowDefinitionYamlBlocksItem.FileUpload | WorkflowDefinitionYamlBlocksItem.FileUrlParser | WorkflowDefinitionYamlBlocksItem.ForLoop | WorkflowDefinitionYamlBlocksItem.GotoUrl | WorkflowDefinitionYamlBlocksItem.HttpRequest | WorkflowDefinitionYamlBlocksItem.Login | WorkflowDefinitionYamlBlocksItem.Navigation | WorkflowDefinitionYamlBlocksItem.PdfParser | WorkflowDefinitionYamlBlocksItem.SendEmail | WorkflowDefinitionYamlBlocksItem.Task | WorkflowDefinitionYamlBlocksItem.TaskV2 | WorkflowDefinitionYamlBlocksItem.TextPrompt | WorkflowDefinitionYamlBlocksItem.UploadToS3 | WorkflowDefinitionYamlBlocksItem.Validation | WorkflowDefinitionYamlBlocksItem.Wait;
    interface Action extends ActionBlockYaml.Raw {
        block_type: "action";
    }
    interface Code extends CodeBlockYaml.Raw {
        block_type: "code";
    }
    interface DownloadToS3 extends DownloadToS3BlockYaml.Raw {
        block_type: "download_to_s3";
    }
    interface Extraction extends ExtractionBlockYaml.Raw {
        block_type: "extraction";
    }
    interface FileDownload extends FileDownloadBlockYaml.Raw {
        block_type: "file_download";
    }
    interface FileUpload extends FileUploadBlockYaml.Raw {
        block_type: "file_upload";
    }
    interface FileUrlParser extends FileParserBlockYaml.Raw {
        block_type: "file_url_parser";
    }
    interface ForLoop extends serializers.ForLoopBlockYaml.Raw {
        block_type: "for_loop";
    }
    interface GotoUrl extends UrlBlockYaml.Raw {
        block_type: "goto_url";
    }
    interface HttpRequest extends HttpRequestBlockYaml.Raw {
        block_type: "http_request";
    }
    interface Login extends LoginBlockYaml.Raw {
        block_type: "login";
    }
    interface Navigation extends NavigationBlockYaml.Raw {
        block_type: "navigation";
    }
    interface PdfParser extends PdfParserBlockYaml.Raw {
        block_type: "pdf_parser";
    }
    interface SendEmail extends SendEmailBlockYaml.Raw {
        block_type: "send_email";
    }
    interface Task extends TaskBlockYaml.Raw {
        block_type: "task";
    }
    interface TaskV2 extends TaskV2BlockYaml.Raw {
        block_type: "task_v2";
    }
    interface TextPrompt extends TextPromptBlockYaml.Raw {
        block_type: "text_prompt";
    }
    interface UploadToS3 extends UploadToS3BlockYaml.Raw {
        block_type: "upload_to_s3";
    }
    interface Validation extends ValidationBlockYaml.Raw {
        block_type: "validation";
    }
    interface Wait extends WaitBlockYaml.Raw {
        block_type: "wait";
    }
}
