import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { type FileUploadNode } from "./types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { useState } from "react";

function FileUploadNode({ id, data }: NodeProps<FileUploadNode>) {
  const { updateNodeData } = useReactFlow();
  const deleteNodeCallback = useDeleteNodeCallback();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });

  const [inputs, setInputs] = useState({
    storageType: data.storageType,
    awsAccessKeyId: data.awsAccessKeyId,
    awsSecretAccessKey: data.awsSecretAccessKey,
    s3Bucket: data.s3Bucket,
    regionName: data.regionName,
    path: data.path,
  });

  function handleChange(key: string, value: unknown) {
    if (!data.editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  return (
    <div>
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
      <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
        <div className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <WorkflowBlockIcon
                workflowBlockType={WorkflowBlockTypes.UploadToS3}
                className="size-6"
              />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={data.editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">File Upload Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">Storage Type</Label>
              <HelpTooltip
                content={helpTooltips["fileUpload"]["storage_type"]}
              />
            </div>
            <Input
              value={data.storageType}
              className="nopan text-xs"
              disabled
            />
          </div>
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
                handleChange("awsAccessKeyId", value);
              }}
              value={inputs.awsAccessKeyId}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">
                AWS Secret Access Key
              </Label>
              <HelpTooltip
                content={helpTooltips["fileUpload"]["aws_secret_access_key"]}
              />
            </div>
            <Input
              type="password"
              value={inputs.awsSecretAccessKey}
              className="nopan text-xs"
              onChange={(event) => {
                handleChange("awsSecretAccessKey", event.target.value);
              }}
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">S3 Bucket</Label>
              <HelpTooltip content={helpTooltips["fileUpload"]["s3_bucket"]} />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={id}
              onChange={(value) => {
                handleChange("s3Bucket", value);
              }}
              value={inputs.s3Bucket}
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
                handleChange("regionName", value);
              }}
              value={inputs.regionName}
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
                handleChange("path", value);
              }}
              value={inputs.path}
              className="nopan text-xs"
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export { FileUploadNode };
