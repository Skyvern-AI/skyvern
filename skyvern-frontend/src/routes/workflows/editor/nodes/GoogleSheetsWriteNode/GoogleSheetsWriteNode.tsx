import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Handle, NodeProps, Position, useEdges, useNodes } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import type { GoogleSheetsWriteNode as GoogleSheetsWriteNodeType } from "./types";
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
import { ColumnMappingEditor } from "@/routes/workflows/components/ColumnMappingEditor";
import { useGoogleSheetHeaders } from "@/hooks/useGoogleSheetHeaders";
import { useGoogleOAuthCredentials } from "@/hooks/useGoogleOAuthCredentials";
import { useGoogleSpreadsheet } from "@/hooks/useGoogleSpreadsheet";
import { isReconnectRequired } from "@/util/googleSheetsErrors";
import { useState } from "react";

function GoogleSheetsWriteNode({
  id,
  data,
}: NodeProps<GoogleSheetsWriteNodeType>) {
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

  const update = useUpdate<GoogleSheetsWriteNodeType["data"]>({ id, editable });
  const recordingStore = useRecordingStore();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    id,
  );
  const headersQuery = useGoogleSheetHeaders({
    credentialId: data.credentialId,
    spreadsheetUrlOrId: data.spreadsheetUrl,
    sheetName: data.sheetName,
  });
  const needsReconnect = isReconnectRequired(headersQuery.error);
  const rerender = useRerender({ prefix: "google-sheets-write" });
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
          type="google_sheets_write"
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
                    helpTooltips["google_sheets_write"]?.["credentialId"] ??
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
              {needsReconnect ? (
                <a
                  href="/integrations"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 rounded-full border border-amber-600/50 bg-amber-900/30 px-2 py-0.5 text-[0.7rem] text-amber-200 hover:bg-amber-900/50"
                >
                  Reconnect this Google account
                </a>
              ) : null}
            </div>

            {/* Spreadsheet */}
            <div className="space-y-2">
              <div className="flex justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Spreadsheet</Label>
                  <HelpTooltip
                    content={
                      helpTooltips["google_sheets_write"]?.["spreadsheetUrl"] ??
                      "The spreadsheet to write to. Type to search your Google Drive, or paste a spreadsheet URL."
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
                allowCreate={true}
                onChange={(value) => {
                  setSpreadsheetDisplayName(null);
                  const oldId = extractSpreadsheetIdFromUrl(
                    data.spreadsheetUrl,
                  );
                  const newId = extractSpreadsheetIdFromUrl(value);
                  // Reset sheetName whenever the resolved id changes to a
                  // new valid id - including the case where the previous
                  // value was an unparseable intermediate edit (oldId=null)
                  // and the new value is a different real spreadsheet.
                  // Without this, A -> garbage -> B preserves A's sheetName
                  // against B.
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
                  {!data.credentialId || !data.spreadsheetUrl ? (
                    <div className="rounded-md border border-dashed border-slate-700 bg-slate-900/30 p-2 text-[0.7rem] text-slate-400">
                      Pick a Google account and spreadsheet to continue.
                    </div>
                  ) : null}

                  {/* Write Mode */}
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Write Mode
                      </Label>
                      <HelpTooltip
                        content={
                          helpTooltips["google_sheets_write"]?.["writeMode"] ??
                          "Append adds new rows after existing data. Update overwrites the specified range."
                        }
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-2">
                      {(
                        [
                          {
                            key: "append",
                            title: "Append rows",
                            body: "Add new rows below the existing data in this sheet.",
                          },
                          {
                            key: "update",
                            title: "Update range",
                            body: "Overwrite a specific range of cells with new values.",
                          },
                        ] as const
                      ).map((opt) => {
                        const selected = data.writeMode === opt.key;
                        return (
                          <button
                            key={opt.key}
                            type="button"
                            onClick={() =>
                              update(
                                opt.key === "append"
                                  ? { writeMode: opt.key, range: "" }
                                  : { writeMode: opt.key },
                              )
                            }
                            className={cn(
                              "nopan flex flex-col gap-1 rounded-md border px-3 py-2 text-left text-xs transition-colors",
                              selected
                                ? "border-slate-300 bg-slate-800 text-slate-100"
                                : "border-slate-700 bg-slate-900/60 text-slate-300 hover:border-slate-500",
                            )}
                          >
                            <span className="font-medium">{opt.title}</span>
                            <span className="text-[0.7rem] text-slate-400">
                              {opt.body}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>

                  {/* Sheet Name */}
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Sheet Name
                      </Label>
                      <HelpTooltip
                        content={
                          helpTooltips["google_sheets_write"]?.["sheetName"] ??
                          "The sheet tab to write to. Type to search tabs in the selected spreadsheet."
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
                      allowCreate={true}
                      onChange={(value) => update({ sheetName: value })}
                      onSelect={(tabName) => update({ sheetName: tabName })}
                    />
                  </div>

                  {/* Range - only for Update Range mode */}
                  {data.writeMode === "update" ? (
                    <div className="space-y-2">
                      <div className="flex gap-2">
                        <Label className="text-xs text-slate-300">Range</Label>
                        <HelpTooltip
                          content={
                            helpTooltips["google_sheets_write"]?.["range"] ??
                            "The exact cells to overwrite. Data shape must match the range."
                          }
                        />
                      </div>
                      <WorkflowBlockInputTextarea
                        nodeId={id}
                        onChange={(value) => {
                          update({ range: value });
                        }}
                        value={data.range}
                        placeholder="A2:D5 or MyNamedRange"
                        className="nopan text-xs"
                      />
                    </div>
                  ) : null}

                  {/* Values */}
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">Values</Label>
                      <HelpTooltip
                        content={
                          helpTooltips["google_sheets_write"]?.["values"] ??
                          "Jinja2 template that resolves to a JSON array. Arrays of lists write left-to-right; arrays of objects require column mappings below."
                        }
                      />
                    </div>
                    <WorkflowBlockInputTextarea
                      nodeId={id}
                      onChange={(value) => {
                        update({ values: value });
                      }}
                      value={data.values}
                      placeholder={
                        data.writeMode === "append"
                          ? "{{ block_1.output }}  // full fidelity: preserves values, formatting, and merges"
                          : "{{ block_1.output }}  // must match range shape; formatting preserved"
                      }
                      className="nopan text-xs"
                    />
                    {(() => {
                      const raw = data.values?.trim() ?? "";
                      if (!raw) return null;
                      if (raw.includes("{{")) return null;
                      try {
                        JSON.parse(raw);
                        return null;
                      } catch {
                        return (
                          <div className="rounded-md border border-amber-600/40 bg-amber-900/20 px-2 py-1 text-[0.7rem] text-amber-200">
                            Values must be a JSON array of rows or objects, or a
                            Jinja template referencing a block output.
                          </div>
                        );
                      }
                    })()}
                  </div>

                  {/* Column Mapping */}
                  <div className="space-y-2">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Column Mapping
                        {(() => {
                          try {
                            const parsed = data.columnMapping
                              ? JSON.parse(data.columnMapping)
                              : {};
                            const count =
                              typeof parsed === "object" && parsed
                                ? Object.keys(parsed).length
                                : 0;
                            return count === 0
                              ? " - required when writing objects"
                              : ` - ${count} columns mapped`;
                          } catch {
                            return " - required when writing objects";
                          }
                        })()}
                      </Label>
                      <HelpTooltip
                        content={
                          helpTooltips["google_sheets_write"]?.[
                            "columnMapping"
                          ] ??
                          "Map each field in your data to a sheet column. Use the column letter (A, B) or the header name if your sheet has a header row."
                        }
                      />
                    </div>
                    <ColumnMappingEditor
                      idScope={id}
                      value={data.columnMapping}
                      onChange={(next) => update({ columnMapping: next })}
                      headers={headersQuery.data ?? []}
                      headersLoading={headersQuery.isLoading}
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
                  {/* Create sheet if missing */}
                  <div className="flex items-center gap-2">
                    <Checkbox
                      checked={data.createSheetIfMissing}
                      disabled={!data.editable}
                      onCheckedChange={(checked) =>
                        update({
                          createSheetIfMissing:
                            checked === "indeterminate" ? false : checked,
                        })
                      }
                    />
                    <Label className="text-xs text-slate-300">
                      Create sheet if missing
                    </Label>
                    <HelpTooltip content="Auto-create the target sheet tab before writing. Required when looping with a dynamic sheet name like sheet_{{ current_index }}." />
                  </div>
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

export { GoogleSheetsWriteNode };
