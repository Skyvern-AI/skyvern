import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import { type FileUploadNode } from "./types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { useState } from "react";
import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";

function FileUploadNode({ id, data }: NodeProps<FileUploadNode>) {
  const { updateNodeData } = useReactFlow();
  const { debuggable, editable, label } = data;
  const debugStore = useDebugStore();
  const elideFromDebugging = debugStore.isDebugMode && !debuggable;
  const { blockLabel: urlBlockLabel } = useParams();
  const thisBlockIsPlaying =
    urlBlockLabel !== undefined && urlBlockLabel === label;

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
      <div
        className={cn(
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
          {
            "pointer-events-none bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsPlaying,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          disabled={elideFromDebugging}
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
