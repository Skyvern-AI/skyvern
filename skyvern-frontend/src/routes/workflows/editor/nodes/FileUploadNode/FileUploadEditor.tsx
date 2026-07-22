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
    prompt,
    s3Bucket,
    awsAccessKeyId,
    awsSecretAccessKey,
    regionName,
    azureStorageAccountName,
    azureStorageAccountKey,
    azureBlobContainerName,
    googleCredentialId,
    googleDriveFolderId,
    sftpHost,
    sftpPort,
    sftpUsername,
    sftpPassword,
    sftpPrivateKey,
    sftpPrivateKeyPassphrase,
    sftpRemotePath,
    sftpHostKey,
  } = data;
  const update = useUpdate<FileUploadNodeData>({ id: blockId, editable });

  return (
    <div data-testid="file-upload-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label className="text-sm text-muted-foreground">Storage Type</Label>
          <HelpTooltip content={helpTooltips["fileUpload"]["storage_type"]} />
        </div>
        <Select
          value={storageType}
          onValueChange={(value) =>
            value &&
            update({
              storageType: value as "s3" | "azure" | "google_drive" | "sftp",
            })
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
            <SelectItem value="sftp">SFTP</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Label className="text-sm text-muted-foreground">Prompt</Label>
          <HelpTooltip content={helpTooltips["fileUpload"]["prompt"]} />
        </div>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          onChange={(value) => update({ prompt: value })}
          value={prompt ?? ""}
          placeholder={
            'e.g. Only upload the PDF files whose names contain "invoice"'
          }
          className="nopan text-xs"
        />
        <p className="text-xs text-muted-foreground">
          Optional. Leave empty to upload all downloaded files.
        </p>
      </div>

      {storageType === "s3" && (
        <>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">
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
              <Label className="text-sm text-muted-foreground">
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
              <Label className="text-sm text-muted-foreground">S3 Bucket</Label>
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
              <Label className="text-sm text-muted-foreground">
                Region Name
              </Label>
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
              <Label className="text-sm text-muted-foreground">
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
              <Label className="text-sm text-muted-foreground">
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
              <Label className="text-sm text-muted-foreground">
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
              <Label className="text-sm text-muted-foreground">
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
              <Label className="text-sm text-muted-foreground">
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
              <Label className="text-sm text-muted-foreground">
                Google Account
              </Label>
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
              <Label className="text-sm text-muted-foreground">
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

      {storageType === "sftp" && (
        <>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">SFTP Host</Label>
              <HelpTooltip content="The SFTP host to upload files to." />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => update({ sftpHost: value })}
              value={sftpHost ?? ""}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">Port</Label>
              <HelpTooltip content="Numeric only — template values are not supported. Defaults to 22 if left blank." />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) =>
                update({ sftpPort: value.replace(/[^0-9]/g, "") })
              }
              value={sftpPort ?? ""}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">Username</Label>
              <HelpTooltip content="The SFTP username." />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => update({ sftpUsername: value })}
              value={sftpUsername ?? ""}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">Password</Label>
              <HelpTooltip content="Password auth. Leave blank if using a private key. Reference a secret parameter for security." />
            </div>
            <WorkflowBlockInput
              nodeId={blockId}
              type="password"
              onChange={(value) => update({ sftpPassword: value })}
              value={sftpPassword ?? ""}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">
                Private Key
              </Label>
              <HelpTooltip content="PEM private key for key-based auth. Leave blank if using a password. Reference a secret parameter for security." />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => update({ sftpPrivateKey: value })}
              value={sftpPrivateKey ?? ""}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">
                Private Key Passphrase (Optional)
              </Label>
              <HelpTooltip content="Optional passphrase for the private key." />
            </div>
            <WorkflowBlockInput
              nodeId={blockId}
              type="password"
              onChange={(value) => update({ sftpPrivateKeyPassphrase: value })}
              value={sftpPrivateKeyPassphrase ?? ""}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">
                (Optional) Remote Directory
              </Label>
              <HelpTooltip content="Remote directory to upload into. Created if it does not exist. Defaults to the login directory." />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => update({ sftpRemotePath: value })}
              value={sftpRemotePath ?? ""}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Label className="text-sm text-muted-foreground">
                (Optional) Host Key
              </Label>
              <HelpTooltip content="If blank, the server's host key is NOT verified and the connection can be intercepted (MITM). Pin a host key (e.g. 'ssh-ed25519 AAAA...') for untrusted networks." />
            </div>
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => update({ sftpHostKey: value })}
              value={sftpHostKey ?? ""}
              className="nopan text-xs"
            />
          </div>
        </>
      )}
    </div>
  );
}

export { FileUploadEditor };
