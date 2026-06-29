import { useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Label } from "@/components/ui/label";
import { GoogleOAuthCredentialSelector } from "@/routes/workflows/components/GoogleOAuthCredentialSelector";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { GOOGLE_DRIVE_REQUIRED_SCOPES } from "@/util/googleScopes";

import { helpTooltips } from "../../helpContent";
import { type FileUploadNode, type FileUploadNodeData } from "./types";
import { useUpdate } from "../../useUpdate";

function FileUploadEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<FileUploadNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "fileUpload") {
    return null;
  }
  return <FileUploadEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function FileUploadEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: FileUploadNodeData;
}) {
  const {
    editable,
    storageType,
    path,
    s3Bucket,
    awsAccessKeyId,
    awsSecretAccessKey,
    regionName,
    azureStorageAccountName,
    azureStorageAccountKey,
    azureBlobContainerName,
    googleCredentialId,
    googleDriveFolderId,
  } = data;
  const update = useUpdate<FileUploadNodeData>({ id: blockId, editable });

  return (
    <div data-testid="file-upload-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label className="text-sm text-slate-400">Storage Type</Label>
          <HelpTooltip content={helpTooltips["fileUpload"]["storage_type"]} />
        </div>
        <Select
          value={storageType}
          onValueChange={(value) =>
            value &&
            update({ storageType: value as "s3" | "azure" | "google_drive" })
          }
          disabled={!editable}
        >
          <SelectTrigger className="nopan text-xs">
            <SelectValue placeholder="Select storage type" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="s3">Amazon S3</SelectItem>
            <SelectItem value="azure">Azure Blob Storage</SelectItem>
            <SelectItem value="google_drive">Google Drive</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {storageType === "s3" && (
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
              nodeId={blockId}
              onChange={(value) => update({ awsAccessKeyId: value })}
              value={awsAccessKeyId as string}
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
            <WorkflowBlockInput
              nodeId={blockId}
              type="password"
              value={awsSecretAccessKey as string}
              className="nopan text-xs"
              onChange={(value) => update({ awsSecretAccessKey: value })}
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">S3 Bucket</Label>
              <HelpTooltip content={helpTooltips["fileUpload"]["s3_bucket"]} />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => update({ s3Bucket: value })}
              value={s3Bucket as string}
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
              nodeId={blockId}
              onChange={(value) => update({ regionName: value })}
              value={regionName as string}
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
              nodeId={blockId}
              onChange={(value) => update({ path: value })}
              value={path as string}
              className="nopan text-xs"
            />
          </div>
        </>
      )}

      {storageType === "azure" && (
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
              nodeId={blockId}
              onChange={(value) => update({ azureStorageAccountName: value })}
              value={azureStorageAccountName as string}
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
              nodeId={blockId}
              type="password"
              value={azureStorageAccountKey as string}
              className="nopan text-xs"
              onChange={(value) => update({ azureStorageAccountKey: value })}
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
              nodeId={blockId}
              onChange={(value) => update({ azureBlobContainerName: value })}
              value={azureBlobContainerName as string}
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
              nodeId={blockId}
              onChange={(value) => update({ path: value })}
              value={path as string}
              className="nopan text-xs"
            />
          </div>
        </>
      )}

      {storageType === "google_drive" && (
        <>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">Google Account</Label>
              <HelpTooltip content="The connected Google account used for Drive uploads." />
            </div>
            <GoogleOAuthCredentialSelector
              nodeId={blockId}
              value={googleCredentialId ?? ""}
              onChange={(value) => update({ googleCredentialId: value })}
              requiredScopes={GOOGLE_DRIVE_REQUIRED_SCOPES}
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-slate-400">
                Google Drive Folder ID (Required)
              </Label>
              <HelpTooltip content="Required destination Google Drive folder ID. You can paste a Drive folder URL or a bare folder ID." />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => update({ googleDriveFolderId: value })}
              value={googleDriveFolderId ?? ""}
              className="nopan text-xs"
            />
          </div>
        </>
      )}
    </div>
  );
}

export { FileUploadEditor };
