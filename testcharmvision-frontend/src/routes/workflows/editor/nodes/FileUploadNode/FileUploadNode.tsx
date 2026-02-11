import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import { type FileUploadNode } from "./types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { useRecordingStore } from "@/store/useRecordingStore";

function FileUploadNode({ id, data }: NodeProps<FileUploadNode>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const update = useUpdate<FileUploadNode["data"]>({ id, editable });
  const recordingStore = useRecordingStore();

  return (
    <div
      className={cn({
        "pointer-events-none opacity-50": recordingStore.isRecording,
      })}
    >
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <Handle
        type="target"
        position={Position.Top}
        id="b"
        className="opacity-0"
      />
      <div
        className={cn(
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
          {
            "pointer-events-none": thisBlockIsPlaying,
            "bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsTargetted,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="file_upload" // sic: the naming is not consistent
        />
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">Storage Type</Label>
              <HelpTooltip
                content={helpTooltips["fileUpload"]["storage_type"]}
              />
            </div>
            <Select
              value={data.storageType}
              onValueChange={(value) =>
                value && update({ storageType: value as "s3" | "azure" })
              }
              disabled={!editable}
            >
              <SelectTrigger className="nopan text-xs">
                <SelectValue placeholder="Select storage type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="s3">Amazon S3</SelectItem>
                <SelectItem value="azure">Azure Blob Storage</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {data.storageType === "s3" && (
            <>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">
                    AWS Access Key ID
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["fileUpload"]["aws_access_key_id"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={id}
                  onChange={(value) => {
                    update({ awsAccessKeyId: value });
                  }}
                  value={data.awsAccessKeyId as string}
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">
                    AWS Secret Access Key
                  </Label>
                  <HelpTooltip
                    content={
                      helpTooltips["fileUpload"]["aws_secret_access_key"]
                    }
                  />
                </div>
                <WorkflowBlockInput
                  nodeId={id}
                  type="password"
                  value={data.awsSecretAccessKey as string}
                  className="nopan text-xs"
                  onChange={(value) => {
                    update({ awsSecretAccessKey: value });
                  }}
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">S3 Bucket</Label>
                  <HelpTooltip
                    content={helpTooltips["fileUpload"]["s3_bucket"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={id}
                  onChange={(value) => {
                    update({ s3Bucket: value });
                  }}
                  value={data.s3Bucket as string}
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">Region Name</Label>
                  <HelpTooltip
                    content={helpTooltips["fileUpload"]["region_name"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={id}
                  onChange={(value) => {
                    update({ regionName: value });
                  }}
                  value={data.regionName as string}
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">
                    (Optional) Folder Path
                  </Label>
                  <HelpTooltip content={helpTooltips["fileUpload"]["path"]} />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={id}
                  onChange={(value) => {
                    update({ path: value });
                  }}
                  value={data.path as string}
                  className="nopan text-xs"
                />
              </div>
            </>
          )}

          {data.storageType === "azure" && (
            <>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">
                    Storage Account Name
                  </Label>
                  <HelpTooltip
                    content={
                      helpTooltips["fileUpload"]["azure_storage_account_name"]
                    }
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={id}
                  onChange={(value) => {
                    update({ azureStorageAccountName: value });
                  }}
                  value={data.azureStorageAccountName as string}
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">
                    Storage Account Key
                  </Label>
                  <HelpTooltip
                    content={
                      helpTooltips["fileUpload"]["azure_storage_account_key"]
                    }
                  />
                </div>
                <WorkflowBlockInput
                  nodeId={id}
                  type="password"
                  value={data.azureStorageAccountKey as string}
                  className="nopan text-xs"
                  onChange={(value) => {
                    update({ azureStorageAccountKey: value });
                  }}
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">
                    Blob Container Name
                  </Label>
                  <HelpTooltip
                    content={
                      helpTooltips["fileUpload"]["azure_blob_container_name"]
                    }
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={id}
                  onChange={(value) => {
                    update({ azureBlobContainerName: value });
                  }}
                  value={data.azureBlobContainerName as string}
                  className="nopan text-xs"
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Label className="text-sm text-slate-400">
                    (Optional) Folder Path
                  </Label>
                  <HelpTooltip content="Optional folder path within the blob container. Defaults to {{ workflow_run_id }} if not specified." />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={id}
                  onChange={(value) => {
                    update({ path: value });
                  }}
                  value={data.path as string}
                  className="nopan text-xs"
                />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export { FileUploadNode };
