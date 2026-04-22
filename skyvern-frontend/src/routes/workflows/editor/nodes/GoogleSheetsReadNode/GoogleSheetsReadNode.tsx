import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import type { GoogleSheetsReadNode as GoogleSheetsReadNodeType } from "./types";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useRerender } from "@/hooks/useRerender";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { GoogleOAuthCredentialSelector } from "@/routes/workflows/components/GoogleOAuthCredentialSelector";
import { SpreadsheetCombobox } from "@/routes/workflows/components/SpreadsheetCombobox";
import { SheetTabCombobox } from "@/routes/workflows/components/SheetTabCombobox";
import {
  extractSpreadsheetIdFromUrl,
  isTemplateExpression,
} from "@/util/googleSheetsUrl";
import { useGoogleOAuthCredentials } from "@/hooks/useGoogleOAuthCredentials";
import { useGoogleSpreadsheet } from "@/hooks/useGoogleSpreadsheet";
import { useState } from "react";

function GoogleSheetsReadNode({
  id,
  data,
}: NodeProps<GoogleSheetsReadNodeType>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

  const update = useUpdate<GoogleSheetsReadNodeType["data"]>({ id, editable });
  const recordingStore = useRecordingStore();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    id,
  );
  const rerender = useRerender({ prefix: "google-sheets-read" });
  const [spreadsheetDisplayName, setSpreadsheetDisplayName] = useState<
    string | null
  >(null);
  const { credentials } = useGoogleOAuthCredentials();
  const hasSelectedAccount =
    isTemplateExpression(data.credentialId) ||
    credentials.some((c) => c.id === data.credentialId);

  // Rehydrate the human spreadsheet name on workflow reload: the block only
  // persists the URL, so without this the combobox would render the raw URL
  // until the user reopens the picker.
  const extractedSpreadsheetId = extractSpreadsheetIdFromUrl(
    data.spreadsheetUrl,
  );
  const { data: resolvedSpreadsheet } = useGoogleSpreadsheet({
    credentialId: data.credentialId,
    spreadsheetId: extractedSpreadsheetId,
  });
  const effectiveDisplayName =
    spreadsheetDisplayName ?? resolvedSpreadsheet?.name ?? null;

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
          type="google_sheets_read"
        />

        <div className="space-y-4">
          {/* Connection Section */}
          <div className="space-y-3">
            <div className="text-xs font-medium uppercase tracking-wider text-slate-400">
              Connection
            </div>

            {/* Google Account */}
            <div className="space-y-2">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">Google Account</Label>
                <HelpTooltip
                  content={
                    helpTooltips["google_sheets_read"]?.["credentialId"] ??
                    "The Google account used to authenticate with the spreadsheet"
                  }
                />
              </div>
              <GoogleOAuthCredentialSelector
                nodeId={id}
                value={data.credentialId}
                onChange={(value) => {
                  update({ credentialId: value });
                }}
              />
            </div>

            {/* Spreadsheet */}
            <div className="space-y-2">
              <div className="flex justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Spreadsheet</Label>
                  <HelpTooltip
                    content={
                      helpTooltips["google_sheets_read"]?.["spreadsheetUrl"] ??
                      "The spreadsheet to read. Type to search your Google Drive, or paste a spreadsheet URL."
                    }
                  />
                </div>
                {isFirstWorkflowBlock ? (
                  <div className="flex justify-end text-xs text-slate-400">
                    Tip: Use the {"+"} button to add parameters!
                  </div>
                ) : null}
              </div>
              <SpreadsheetCombobox
                nodeId={id}
                credentialId={data.credentialId}
                hasSelectedAccount={hasSelectedAccount}
                value={data.spreadsheetUrl}
                displayName={effectiveDisplayName}
                placeholder="Search or paste a spreadsheet URL"
                allowCreate={false}
                onChange={(value) => {
                  setSpreadsheetDisplayName(null);
                  const oldId = extractSpreadsheetIdFromUrl(
                    data.spreadsheetUrl,
                  );
                  const newId = extractSpreadsheetIdFromUrl(value);
                  // Same logic as the Write node: clear sheetName whenever
                  // the resolved id changes to a new valid id, even if the
                  // previous value was an unparseable intermediate edit.
                  const spreadsheetSwitched = newId !== null && newId !== oldId;
                  update(
                    spreadsheetSwitched
                      ? { spreadsheetUrl: value, sheetName: "" }
                      : { spreadsheetUrl: value },
                  );
                }}
                onSelect={(selection) => {
                  setSpreadsheetDisplayName(selection.name);
                  update({
                    spreadsheetUrl: selection.url,
                    sheetName: selection.firstSheetName ?? "",
                  });
                }}
              />
            </div>
          </div>

          <Separator />

          <Accordion
            type="multiple"
            defaultValue={["data"]}
            onValueChange={() => rerender.bump()}
          >
            <AccordionItem value="data" className="border-b-0">
              <AccordionTrigger className="py-2">Data</AccordionTrigger>
              <AccordionContent className="pl-6 pr-1 pt-4">
                <div key={`${rerender.key}-data`} className="space-y-3">
                  {/* Sheet Name */}
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Sheet Name
                      </Label>
                      <HelpTooltip
                        content={
                          helpTooltips["google_sheets_read"]?.["sheetName"] ??
                          "The sheet tab to read. Type to search tabs in the selected spreadsheet."
                        }
                      />
                    </div>
                    <SheetTabCombobox
                      nodeId={id}
                      credentialId={data.credentialId}
                      hasSelectedAccount={hasSelectedAccount}
                      spreadsheetUrl={data.spreadsheetUrl}
                      value={data.sheetName}
                      placeholder="Sheet1"
                      allowCreate={false}
                      onChange={(value) => update({ sheetName: value })}
                      onSelect={(tabName) => update({ sheetName: tabName })}
                    />
                  </div>

                  {/* Range */}
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">Range</Label>
                      <HelpTooltip
                        content={
                          helpTooltips["google_sheets_read"]?.["range"] ??
                          "A1 notation range to read (optional, defaults to all data). Examples: A1:D10 for a specific range, MyNamedRange for named ranges, or leave empty for all rows."
                        }
                      />
                    </div>
                    <WorkflowBlockInputTextarea
                      nodeId={id}
                      onChange={(value) => {
                        update({ range: value });
                      }}
                      value={data.range}
                      placeholder="A1:D10, MyNamedRange, or leave empty for all rows"
                      className="nopan text-xs"
                    />
                  </div>

                  {/* Has Header Row */}
                  <div className="flex items-center justify-between">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Has Header Row
                      </Label>
                      <HelpTooltip
                        content={
                          helpTooltips["google_sheets_read"]?.[
                            "hasHeaderRow"
                          ] ??
                          "If enabled, the first row is used as column headers for the output objects"
                        }
                      />
                    </div>
                    <Switch
                      checked={data.hasHeaderRow}
                      onCheckedChange={(checked) => {
                        update({ hasHeaderRow: checked });
                      }}
                    />
                  </div>
                </div>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="advanced" className="border-b-0">
              <AccordionTrigger className="py-2">
                Advanced Settings
              </AccordionTrigger>
              <AccordionContent className="pl-6 pr-1 pt-4">
                <div key={`${rerender.key}-advanced`} className="space-y-3">
                  <ParametersMultiSelect
                    availableOutputParameters={availableOutputParameterKeys}
                    parameters={data.parameterKeys}
                    onParametersChange={(parameterKeys) => {
                      update({ parameterKeys });
                    }}
                  />
                </div>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>
      </div>
    </div>
  );
}

export { GoogleSheetsReadNode };
