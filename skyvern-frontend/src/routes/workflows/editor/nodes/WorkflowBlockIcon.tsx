import { ClickIcon } from "@/components/icons/ClickIcon";
import { WorkflowBlockType } from "../../types/workflowTypes";
import {
  CheckCircledIcon,
  CodeIcon,
  CursorTextIcon,
  DownloadIcon,
  EnvelopeClosedIcon,
  ExternalLinkIcon,
  FileTextIcon,
  GlobeIcon,
  HandIcon,
  ListBulletIcon,
  LockOpen1Icon,
  StopwatchIcon,
  UpdateIcon,
  UploadIcon,
} from "@radix-ui/react-icons";
import { ExtractIcon } from "@/components/icons/ExtractIcon";
import { GitBranchIcon } from "@/components/icons/GitBranchIcon";
import { RobotIcon } from "@/components/icons/RobotIcon";

type Props = {
  workflowBlockType: WorkflowBlockType;
  className?: string;
};

function WorkflowBlockIcon({ workflowBlockType, className }: Props) {
  switch (workflowBlockType) {
    case "action": {
      return <ClickIcon className={className} />;
    }
    case "code": {
      return <CodeIcon className={className} />;
    }
    case "conditional": {
      return <GitBranchIcon className={className} />;
    }
    case "download_to_s3": {
      return <DownloadIcon className={className} />;
    }
    case "extraction": {
      return <ExtractIcon className={className} />;
    }
    case "file_download": {
      return <DownloadIcon className={className} />;
    }
    case "file_url_parser": {
      return <CursorTextIcon className={className} />;
    }
    case "for_loop": {
      return <UpdateIcon className={className} />;
    }
    case "login": {
      return <LockOpen1Icon className={className} />;
    }
    case "navigation":
    case "task_v2": {
      return <RobotIcon className={className} />;
    }
    case "send_email": {
      return <EnvelopeClosedIcon className={className} />;
    }
    case "task": {
      return <ListBulletIcon className={className} />;
    }
    case "text_prompt": {
      return <CursorTextIcon className={className} />;
    }
    case "upload_to_s3": {
      return <UploadIcon className={className} />;
    }
    case "file_upload": {
      return <UploadIcon className={className} />;
    }
    case "validation": {
      return <CheckCircledIcon className={className} />;
    }
    case "human_interaction": {
      return <HandIcon className={className} />;
    }
    case "wait": {
      return <StopwatchIcon className={className} />;
    }
    case "pdf_parser": {
      return <FileTextIcon className={className} />;
    }
    case "goto_url": {
      return <ExternalLinkIcon className={className} />;
    }
    case "http_request": {
      return <GlobeIcon className={className} />;
    }
  }
}

export { WorkflowBlockIcon };
