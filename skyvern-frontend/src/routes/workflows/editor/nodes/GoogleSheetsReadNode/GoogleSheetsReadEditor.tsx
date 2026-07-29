import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import { useState } from "react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import {
  GOOGLE_SHEETS_REQUIRED_SCOPES,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import { useGoogleSpreadsheet } from "@/hooks/useGoogleSpreadsheet";
import { GoogleOAuthCredentialSelector } from "@/routes/workflows/components/GoogleOAuthCredentialSelector";
import { SheetTabCombobox } from "@/routes/workflows/components/SheetTabCombobox";
import { SpreadsheetCombobox } from "@/routes/workflows/components/SpreadsheetCombobox";
import {
  extractSpreadsheetIdFromUrl,
  isTemplateExpression,
} from "@/util/googleSheetsUrl";

import { helpTooltips } from "../../helpContent";
import { type AppNode } from "..";
import {
  type GoogleSheetsReadNode,
  type GoogleSheetsReadNodeData,
} from "./types";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useUpdate } from "../../useUpdate";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";

function GoogleSheetsReadEditor({ blockId }: { blockId: string }) {
  // Subscribe to this node's data slice. The sidebar mount lives outside the
  // per-node renderer, so a useReactFlow().getNode(id) snapshot does not
  // re-render after updateNodeData commits typed input.
  const nodeSlice = useNodesData<GoogleSheetsReadNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "googleSheetsRead") {
    return null;
  }
  return <GoogleSheetsReadEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function GoogleSheetsReadEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: GoogleSheetsReadNodeData;
}) {
  const { editable } = data;

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );

  const update = useUpdate<GoogleSheetsReadNodeData>({ id: blockId, editable });

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
    <div data-testid="google-sheets-read-block-form" className="space-y-4">
      <div className="space-y-3">
        <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          Connection
        </div>

        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-tertiary-foreground">
              Google Account
            </Label>
            <HelpTooltip
              content={
                helpTooltips["google_sheets_read"]?.["credentialId"] ??
                "The Google account used to authenticate with the spreadsheet"
              }
            />
          </div>
          <GoogleOAuthCredentialSelector
            nodeId={blockId}
            value={data.credentialId}
            onChange={(next) => update({ credentialId: next })}
            requiredScopes={GOOGLE_SHEETS_REQUIRED_SCOPES}
          />
        </div>

        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-tertiary-foreground">
              Spreadsheet
            </Label>
            <HelpTooltip
              content={
                helpTooltips["google_sheets_read"]?.["spreadsheetUrl"] ??
                "The spreadsheet to read. Type to search your Google Drive, or paste a spreadsheet URL."
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
            allowCreate={false}
            blockType="google_sheets_read"
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
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
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
                  nodeId={blockId}
                  credentialId={data.credentialId}
                  hasSelectedAccount={hasSelectedAccount}
                  spreadsheetUrl={data.spreadsheetUrl}
                  value={data.sheetName}
                  placeholder="Sheet1"
                  allowCreate={false}
                  blockType="google_sheets_read"
                  onChange={(next) => update({ sheetName: next })}
                  onSelect={(tabName) => update({ sheetName: tabName })}
                />
              </div>

              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    Range
                  </Label>
                  <HelpTooltip
                    content={
                      helpTooltips["google_sheets_read"]?.["range"] ??
                      "A1 notation range to read (optional, defaults to all data). Examples: A1:D10 for a specific range, MyNamedRange for named ranges, or leave empty for all rows."
                    }
                  />
                </div>
                <WorkflowBlockInputTextarea
                  nodeId={blockId}
                  onChange={(next) => update({ range: next })}
                  value={data.range}
                  placeholder="A1:D10, MyNamedRange, or leave empty for all rows"
                  className="nopan text-xs"
                />
              </div>

              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  <Label className="text-xs text-tertiary-foreground">
                    Has Header Row
                  </Label>
                  <HelpTooltip
                    content={
                      helpTooltips["google_sheets_read"]?.["hasHeaderRow"] ??
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
            <div className="space-y-3">
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

export { GoogleSheetsReadEditor };
