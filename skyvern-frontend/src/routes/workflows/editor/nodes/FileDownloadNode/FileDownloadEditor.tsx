import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import type { ReactNode } from "react";

import { BROWSER_DOWNLOAD_TIMEOUT_SECONDS } from "@/api/types";
import { RunEngineSelector } from "@/components/EngineSelector";
import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { GoogleOAuthCredentialSelector } from "@/routes/workflows/components/GoogleOAuthCredentialSelector";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { GOOGLE_DRIVE_REQUIRED_SCOPES } from "@/util/googleScopes";

import { ErrorCodeMappingEditor } from "../../ErrorCodeMappingEditor";
import { AI_IMPROVE_CONFIGS } from "../../constants";
import { helpTooltips, placeholders } from "../../helpContent";
import { useHasInteractedThisSession } from "../../panels/useHasInteractedThisSession";
import { type AppNode } from "..";
import { DisableCache } from "../DisableCache";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { BlockExecutionOptions } from "../components/BlockExecutionOptions";
import { type FileDownloadNode, type FileDownloadNodeData } from "./types";
import { useSelectedCredentialTotpIdentifier } from "../../hooks/useSelectedCredentialTotpIdentifier";
import { errorMappingExampleValue } from "../types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useUpdate } from "../../useUpdate";
import {
  getAvailableOutputParameterKeys,
  getParentLoopSkipsOnFail,
  isNodeInsideForLoop,
} from "../../workflowEditorUtils";

const urlTooltip =
  "The URL Skyvern is navigating to. Leave this field blank to pick up from where the last block left off.";
const urlPlaceholder = "https://";
const navigationGoalTooltip =
  "Give Skyvern an objective that describes how to download the file.";
const navigationGoalPlaceholder = "Tell Skyvern which file to download.";

type DestinationFieldProps = {
  label: string;
  help: string;
  children: ReactNode;
};

function DestinationField({ label, help, children }: DestinationFieldProps) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Label className="text-sm text-slate-400">{label}</Label>
        <HelpTooltip content={help} />
      </div>
      {children}
    </div>
  );
}

function FileDownloadEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<FileDownloadNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "fileDownload") {
    return null;
  }
  return <FileDownloadEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function FileDownloadEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: FileDownloadNodeData;
}) {
  const {
    editable,
    label,
    url,
    navigationGoal,
    downloadTimeout,
    model,
    parameterKeys,
    engine,
    maxStepsOverride,
    errorCodeMapping,
    continueOnFailure,
    nextLoopOnFailure,
    disableCache,
    downloadSuffix,
    totpIdentifier,
    totpVerificationUrl,
    downloadTarget,
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

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const isInsideForLoop = isNodeInsideForLoop(nodes, blockId);
  const parentLoopSkipsOnFail = getParentLoopSkipsOnFail(nodes, blockId);
  const update = useUpdate<FileDownloadNodeData>({ id: blockId, editable });
  const hasInteracted = useHasInteractedThisSession();
  const credentialTotpIdentifier = useSelectedCredentialTotpIdentifier(
    parameterKeys.length > 0 ? parameterKeys[0] : undefined,
  );

  return (
    <div data-testid="file-download-block-form" className="space-y-4">
      <div className="space-y-4">
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Label className="text-sm text-slate-400">Download Target</Label>
            <HelpTooltip
              content={helpTooltips["fileDownload"]["download_target"]}
            />
          </div>
          <Select
            value={downloadTarget}
            onValueChange={(value) =>
              value &&
              update({
                downloadTarget: value as
                  | "website"
                  | "s3"
                  | "azure"
                  | "google_drive"
                  | "sftp",
              })
            }
            disabled={!editable}
          >
            <SelectTrigger className="nopan text-xs">
              <SelectValue placeholder="Select download target" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="website">Website</SelectItem>
              <SelectItem value="s3">AWS S3</SelectItem>
              <SelectItem value="azure">Azure Blob Storage</SelectItem>
              <SelectItem value="google_drive">Google Drive</SelectItem>
              <SelectItem value="sftp">SFTP</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {downloadTarget !== "website" && (
          <DestinationField
            label="Prompt"
            help={helpTooltips["fileDownload"]["prompt"]}
          >
            <WorkflowBlockInputTextarea
              nodeId={blockId}
              onChange={(value) => update({ prompt: value })}
              value={prompt ?? ""}
              placeholder={
                'e.g. Only send the PDF files whose names contain "invoice"'
              }
              className="nopan text-xs"
            />
            <p className="text-xs text-slate-400">
              Optional. Leave empty to send all downloaded files.
            </p>
          </DestinationField>
        )}

        {downloadTarget === "s3" && (
          <>
            <DestinationField
              label="AWS Access Key ID"
              help={helpTooltips["fileDownload"]["aws_access_key_id"]}
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ awsAccessKeyId: value })}
                value={awsAccessKeyId as string}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="AWS Secret Access Key"
              help={helpTooltips["fileDownload"]["aws_secret_access_key"]}
            >
              <WorkflowBlockInput
                nodeId={blockId}
                type="password"
                value={awsSecretAccessKey as string}
                className="nopan text-xs"
                onChange={(value) => update({ awsSecretAccessKey: value })}
              />
            </DestinationField>
            <DestinationField
              label="S3 Bucket"
              help={helpTooltips["fileDownload"]["s3_bucket"]}
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ s3Bucket: value })}
                value={s3Bucket as string}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="Region Name"
              help={helpTooltips["fileDownload"]["region_name"]}
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ regionName: value })}
                value={regionName as string}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="(Optional) Folder Path"
              help={helpTooltips["fileDownload"]["path"]}
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ path: value })}
                value={path as string}
                className="nopan text-xs"
              />
            </DestinationField>
          </>
        )}

        {downloadTarget === "azure" && (
          <>
            <DestinationField
              label="Storage Account Name"
              help={helpTooltips["fileDownload"]["azure_storage_account_name"]}
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ azureStorageAccountName: value })}
                value={azureStorageAccountName as string}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="Storage Account Key"
              help={helpTooltips["fileDownload"]["azure_storage_account_key"]}
            >
              <WorkflowBlockInput
                nodeId={blockId}
                type="password"
                value={azureStorageAccountKey as string}
                className="nopan text-xs"
                onChange={(value) => update({ azureStorageAccountKey: value })}
              />
            </DestinationField>
            <DestinationField
              label="Blob Container Name"
              help={helpTooltips["fileDownload"]["azure_blob_container_name"]}
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ azureBlobContainerName: value })}
                value={azureBlobContainerName as string}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="(Optional) Folder Path"
              help="Optional folder path within the blob container. Defaults to {{ workflow_run_id }} if not specified."
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ path: value })}
                value={path as string}
                className="nopan text-xs"
              />
            </DestinationField>
          </>
        )}

        {downloadTarget === "google_drive" && (
          <>
            <DestinationField
              label="Google Account"
              help="The connected Google account used for Drive uploads."
            >
              <GoogleOAuthCredentialSelector
                nodeId={blockId}
                value={googleCredentialId ?? ""}
                onChange={(value) => update({ googleCredentialId: value })}
                requiredScopes={GOOGLE_DRIVE_REQUIRED_SCOPES}
              />
            </DestinationField>
            <DestinationField
              label="Google Drive Folder ID (Required)"
              help="Required destination Google Drive folder ID. You can paste a Drive folder URL or a bare folder ID."
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ googleDriveFolderId: value })}
                value={googleDriveFolderId ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
          </>
        )}

        {downloadTarget === "sftp" && (
          <>
            <DestinationField
              label="SFTP Host"
              help="The SFTP host to upload files to."
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ sftpHost: value })}
                value={sftpHost ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="Port"
              help="Numeric only — template values are not supported. Defaults to 22 if left blank."
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) =>
                  update({ sftpPort: value.replace(/[^0-9]/g, "") })
                }
                value={sftpPort ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField label="Username" help="The SFTP username.">
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ sftpUsername: value })}
                value={sftpUsername ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="Password"
              help="Password auth. Leave blank if using a private key. Reference a secret parameter for security."
            >
              <WorkflowBlockInput
                nodeId={blockId}
                type="password"
                onChange={(value) => update({ sftpPassword: value })}
                value={sftpPassword ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="Private Key"
              help="PEM private key for key-based auth. Leave blank if using a password. Reference a secret parameter for security."
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ sftpPrivateKey: value })}
                value={sftpPrivateKey ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="Private Key Passphrase (Optional)"
              help="Optional passphrase for the private key."
            >
              <WorkflowBlockInput
                nodeId={blockId}
                type="password"
                onChange={(value) =>
                  update({ sftpPrivateKeyPassphrase: value })
                }
                value={sftpPrivateKeyPassphrase ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="(Optional) Remote Directory"
              help="Remote directory to upload into. Created if it does not exist. Defaults to the login directory."
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ sftpRemotePath: value })}
                value={sftpRemotePath ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
            <DestinationField
              label="(Optional) Host Key"
              help="If blank, the server's host key is NOT verified and the connection can be intercepted (MITM). Pin a host key (e.g. 'ssh-ed25519 AAAA...') for untrusted networks."
            >
              <WorkflowBlockInputTextarea
                nodeId={blockId}
                onChange={(value) => update({ sftpHostKey: value })}
                value={sftpHostKey ?? ""}
                className="nopan text-xs"
              />
            </DestinationField>
          </>
        )}

        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-tertiary-foreground">URL</Label>
            <HelpTooltip content={urlTooltip} />
          </div>
          <WorkflowBlockInputTextarea
            nodeId={blockId}
            onChange={(next) => update({ url: next })}
            value={url}
            placeholder={urlPlaceholder}
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-tertiary-foreground">
              Download Goal
            </Label>
            <HelpTooltip content={navigationGoalTooltip} />
          </div>
          <WorkflowBlockInputTextarea
            aiImprove={AI_IMPROVE_CONFIGS.fileDownload.navigationGoal}
            nodeId={blockId}
            onChange={(next) => update({ navigationGoal: next })}
            value={navigationGoal}
            placeholder={navigationGoalPlaceholder}
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Label className="text-xs text-tertiary-foreground">
              Download Timeout (sec)
            </Label>
            <HelpTooltip
              content={`The maximum time to wait for downloads to complete, in seconds. If not set, defaults to ${BROWSER_DOWNLOAD_TIMEOUT_SECONDS} seconds.`}
            />
            <Input
              className="ml-auto w-16 text-right"
              type="number"
              min={1}
              value={downloadTimeout ?? ""}
              placeholder={`${BROWSER_DOWNLOAD_TIMEOUT_SECONDS}`}
              onChange={(event) => {
                // Empty input clears the override; numeric inputs land
                // verbatim. The previous `if (next) { update(...) }`
                // silently dropped `0`, so a user typing "0" thinking
                // "no timeout" got no save and no feedback.
                if (event.target.value === "") {
                  update({ downloadTimeout: null });
                  return;
                }
                const next = Number(event.target.value);
                if (Number.isFinite(next) && next >= 0) {
                  update({ downloadTimeout: next });
                }
              }}
            />
          </div>
        </div>
        {!hasInteracted && (
          <div className="workflow-editor-tip rounded-md bg-muted p-2 text-xs text-muted-foreground">
            Once the file is downloaded, this block will complete.
          </div>
        )}
      </div>
      <Separator />
      <Accordion type="single" collapsible>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <div className="space-y-4">
              <div className="space-y-2">
                <ModelSelector
                  className="nopan w-52 text-xs"
                  value={model}
                  onChange={(next) => update({ model: next })}
                />
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={parameterKeys}
                  onParametersChange={(next) => update({ parameterKeys: next })}
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-tertiary-foreground">
                    Engine
                  </Label>
                </div>
                <RunEngineSelector
                  value={engine}
                  onChange={(next) => update({ engine: next })}
                  className="nopan w-52 text-xs"
                />
              </div>
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-tertiary-foreground">
                    Max Steps Override
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["download"]["maxStepsOverride"]}
                  />
                </div>
                <Input
                  type="number"
                  placeholder={placeholders["download"]["maxStepsOverride"]}
                  className="nopan w-52 text-xs"
                  min="0"
                  value={maxStepsOverride ?? ""}
                  onChange={(event) => {
                    const next =
                      event.target.value === ""
                        ? null
                        : Number(event.target.value);
                    update({ maxStepsOverride: next });
                  }}
                />
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-tertiary-foreground">
                      Error Messages
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["download"]["errorCodeMapping"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={errorCodeMapping !== "null"}
                      onCheckedChange={(checked) => {
                        if (!editable) {
                          return;
                        }
                        update({
                          errorCodeMapping: checked
                            ? JSON.stringify(errorMappingExampleValue, null, 2)
                            : "null",
                        });
                      }}
                    />
                  </div>
                </div>
                {errorCodeMapping !== "null" && (
                  <ErrorCodeMappingEditor
                    label={label}
                    value={errorCodeMapping}
                    onChange={(next) => update({ errorCodeMapping: next })}
                    readOnly={!editable}
                  />
                )}
              </div>
              <BlockExecutionOptions
                continueOnFailure={continueOnFailure}
                nextLoopOnFailure={nextLoopOnFailure}
                editable={editable}
                isInsideForLoop={isInsideForLoop}
                parentLoopSkipsOnFail={parentLoopSkipsOnFail}
                blockType="download"
                onContinueOnFailureChange={(checked) =>
                  update({ continueOnFailure: checked })
                }
                onNextLoopOnFailureChange={(checked) =>
                  update({ nextLoopOnFailure: checked })
                }
              />
              <DisableCache
                disableCache={disableCache}
                editable={editable}
                onDisableCacheChange={(next) => update({ disableCache: next })}
              />
              <IgnoreWorkflowSystemPrompt
                ignoreWorkflowSystemPrompt={
                  data.ignoreWorkflowSystemPrompt ?? false
                }
                editable={editable}
                onIgnoreWorkflowSystemPromptChange={(
                  ignoreWorkflowSystemPrompt,
                ) => {
                  update({ ignoreWorkflowSystemPrompt });
                }}
              />
              <Separator />
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-tertiary-foreground">
                    File Name
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["download"]["fileSuffix"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ downloadSuffix: next })}
                  value={downloadSuffix ?? ""}
                  placeholder={placeholders["download"]["downloadSuffix"]}
                  className="nopan text-xs"
                />
              </div>
              <Separator />
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    2FA Identifier
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["download"]["totpIdentifier"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ totpIdentifier: next })}
                  value={totpIdentifier ?? ""}
                  placeholder={
                    !totpIdentifier?.trim() && credentialTotpIdentifier
                      ? `${credentialTotpIdentifier} (from credential)`
                      : placeholders["download"]["totpIdentifier"]
                  }
                  className="nopan text-xs"
                />
                {!totpIdentifier?.trim() && credentialTotpIdentifier ? (
                  <p className="text-xs text-muted-foreground dark:text-slate-500">
                    Leave empty to use the credential's value.
                  </p>
                ) : null}
              </div>
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    2FA Verification URL
                  </Label>
                  <HelpTooltip
                    content={helpTooltips["task"]["totpVerificationUrl"]}
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ totpVerificationUrl: next })}
                  value={totpVerificationUrl ?? ""}
                  placeholder={placeholders["task"]["totpVerificationUrl"]}
                  className="nopan text-xs"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { FileDownloadEditor };
