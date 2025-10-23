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
