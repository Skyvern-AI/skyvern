import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import { useMemo, useState } from "react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useGoogleOAuthCredentials } from "@/hooks/useGoogleOAuthCredentials";
import { useGoogleSheetDimensions } from "@/hooks/useGoogleSheetDimensions";
import { useGoogleSheetHeaders } from "@/hooks/useGoogleSheetHeaders";
import { useGoogleSpreadsheet } from "@/hooks/useGoogleSpreadsheet";
import { ColumnMappingEditor } from "@/routes/workflows/components/ColumnMappingEditor";
import { GoogleOAuthCredentialSelector } from "@/routes/workflows/components/GoogleOAuthCredentialSelector";
import { SheetTabCombobox } from "@/routes/workflows/components/SheetTabCombobox";
import { SpreadsheetCombobox } from "@/routes/workflows/components/SpreadsheetCombobox";
import { parseColumnMapping } from "@/util/columnMappingSerialization";
import {
  columnLettersToIndex,
  extractSpreadsheetIdFromUrl,
  isTemplateExpression,
} from "@/util/googleSheetsUrl";
import { isReconnectRequired } from "@/util/googleSheetsErrors";
import { cn } from "@/util/utils";

import { helpTooltips } from "../../helpContent";
import { type AppNode } from "..";
import {
  type GoogleSheetsWriteNode,
  type GoogleSheetsWriteNodeData,
} from "./types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useUpdate } from "../../useUpdate";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";

function GoogleSheetsWriteEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<GoogleSheetsWriteNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "googleSheetsWrite") {
    return null;
  }
  return (
    <GoogleSheetsWriteEditorBody blockId={blockId} data={nodeSlice.data} />
  );
}

function GoogleSheetsWriteEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: GoogleSheetsWriteNodeData;
}) {
  const { editable } = data;

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );

  const update = useUpdate<GoogleSheetsWriteNodeData>({
    id: blockId,
    editable,
  });

  const headersQuery = useGoogleSheetHeaders({
    credentialId: data.credentialId,
    spreadsheetUrlOrId: data.spreadsheetUrl,
    sheetName: data.sheetName,
  });
  const dimensionsQuery = useGoogleSheetDimensions({
    credentialId: data.credentialId,
    spreadsheetUrlOrId: data.spreadsheetUrl,
    sheetName: data.sheetName,
  });
  const needsReconnect = isReconnectRequired(headersQuery.error);

  const overflowingMappings = useMemo<
    { field: string; letter: string }[]
  >(() => {
    if (!dimensionsQuery.data || !data.columnMapping) return [];
    const lastIndex = columnLettersToIndex(
      dimensionsQuery.data.last_column_letter,
    );
    if (lastIndex <= 0) return [];
    const out: { field: string; letter: string }[] = [];
    for (const { key, letter } of parseColumnMapping(data.columnMapping)) {
      const targetIndex = columnLettersToIndex(letter);
      if (targetIndex > 0 && targetIndex > lastIndex) {
        out.push({ field: key, letter: letter.toUpperCase() });
      }
    }
    return out;
  }, [dimensionsQuery.data, data.columnMapping]);

  const [spreadsheetDisplayName, setSpreadsheetDisplayName] = useState<
    string | null
  >(null);

  const { credentials } = useGoogleOAuthCredentials();
  const hasSelectedAccount =
    isTemplateExpression(data.credentialId) ||
    credentials.some((c) => c.id === data.credentialId);

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
    <div data-testid="google-sheets-write-block-form" className="space-y-4">
      <div className="space-y-3">
        <div className="text-xs font-medium uppercase tracking-wider text-slate-400">
          Connection
        </div>

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
            nodeId={blockId}
            value={data.credentialId}
            onChange={(next) => update({ credentialId: next })}
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

        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Spreadsheet</Label>
            <HelpTooltip
              content={
                helpTooltips["google_sheets_write"]?.["spreadsheetUrl"] ??
                "The spreadsheet to write to. Type to search your Google Drive, or paste a spreadsheet URL."
              }
            />
          </div>
          <SpreadsheetCombobox
            nodeId={blockId}
            credentialId={data.credentialId}
            hasSelectedAccount={hasSelectedAccount}
            value={data.spreadsheetUrl}
            displayName={effectiveDisplayName}
            placeholder="Search or paste a spreadsheet URL"
            allowCreate={true}
            blockType="google_sheets_write"
            onChange={(next) => {
              setSpreadsheetDisplayName(null);
              const oldId = extractSpreadsheetIdFromUrl(data.spreadsheetUrl);
              const newId = extractSpreadsheetIdFromUrl(next);
              const spreadsheetSwitched = newId !== null && newId !== oldId;
              update(
                spreadsheetSwitched
                  ? { spreadsheetUrl: next, sheetName: "" }
                  : { spreadsheetUrl: next },
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

      <Accordion type="multiple" defaultValue={["data"]}>
        <AccordionItem value="data" className="border-b-0">
          <AccordionTrigger className="py-2">Data</AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-4">
            <div className="space-y-3">
              {!data.credentialId || !data.spreadsheetUrl ? (
                <div className="rounded-md border border-dashed border-slate-700 bg-slate-900/30 p-2 text-[0.7rem] text-slate-400">
                  Pick a Google account and spreadsheet to continue.
                </div>
              ) : null}

              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Write Mode</Label>
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

              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Sheet Name</Label>
                  <HelpTooltip
                    content={
                      helpTooltips["google_sheets_write"]?.["sheetName"] ??
                      "The sheet tab to write to. Type to search tabs in the selected spreadsheet."
                    }
                  />
                </div>
                <SheetTabCombobox
                  nodeId={blockId}
                  credentialId={data.credentialId}
                  hasSelectedAccount={hasSelectedAccount}
                  spreadsheetUrl={data.spreadsheetUrl}
                  value={data.sheetName}
                  placeholder="Sheet1"
                  allowCreate={true}
                  blockType="google_sheets_write"
                  onChange={(next) => update({ sheetName: next })}
                  onSelect={(tabName) => update({ sheetName: tabName })}
                />
                {dimensionsQuery.data ? (
                  <div className="space-y-1 rounded-md border border-slate-700 bg-slate-900/40 px-2 py-1.5 text-[0.7rem] text-slate-300">
                    <div>
                      Sheet{" "}
                      <span className="font-medium text-slate-100">
                        "{dimensionsQuery.data.title}"
                      </span>{" "}
                      - {dimensionsQuery.data.column_count} columns (last is{" "}
                      <span className="font-mono">
                        {dimensionsQuery.data.last_column_letter}
                      </span>
                      ) x {dimensionsQuery.data.row_count} rows.
                    </div>
                    {dimensionsQuery.data.headers.length > 0 ? (
                      <details className="text-slate-400">
                        <summary className="cursor-pointer">
                          {dimensionsQuery.data.headers.length} header
                          {dimensionsQuery.data.headers.length === 1
                            ? ""
                            : "s"}{" "}
                          in row 1
                        </summary>
                        <ul className="mt-1 grid grid-cols-2 gap-x-3 pl-2">
                          {dimensionsQuery.data.headers.map((h) => (
                            <li key={h.letter}>
                              <span className="font-mono text-slate-500">
                                {h.letter}
                              </span>{" "}
                              {h.name}
                            </li>
                          ))}
                        </ul>
                      </details>
                    ) : null}
                    {overflowingMappings.length > 0 ? (
                      <div className="rounded border border-amber-600/40 bg-amber-900/20 px-2 py-1 text-amber-200">
                        Column mapping writes past column{" "}
                        <span className="font-mono">
                          {dimensionsQuery.data.last_column_letter}
                        </span>{" "}
                        (
                        {overflowingMappings
                          .map((m) => `${m.field}->${m.letter}`)
                          .join(", ")}
                        ). The sheet will be auto-widened on run.
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </div>

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
                    nodeId={blockId}
                    onChange={(next) => update({ range: next })}
                    value={data.range}
                    placeholder="A2:D5 or MyNamedRange"
                    className="nopan text-xs"
                  />
                </div>
              ) : null}

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
                  nodeId={blockId}
                  onChange={(next) => update({ values: next })}
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
                        This needs to be a JSON array, or a reference to a
                        previous block like {"{{ block_1.output }}"}.
                      </div>
                    );
                  }
                })()}
              </div>

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
                      helpTooltips["google_sheets_write"]?.["columnMapping"] ??
                      "Map each field in your data to a sheet column. Use the column letter (A, B) or the header name if your sheet has a header row."
                    }
                  />
                </div>
                <ColumnMappingEditor
                  idScope={blockId}
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
            <div className="space-y-3">
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
  );
}

export { GoogleSheetsWriteEditor };
