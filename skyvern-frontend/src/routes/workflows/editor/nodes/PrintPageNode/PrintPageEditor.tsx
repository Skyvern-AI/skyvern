import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
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

import { type AppNode } from "..";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { type PrintPageNode, type PrintPageNodeData } from "./types";
import { useUpdate } from "../../useUpdate";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";

function PrintPageEditor({ blockId }: { blockId: string }) {
  // Subscribe to the node's data slice. The sidebar mount lives outside the
  // per-node renderer and the body subscribes to useNodes()/useEdges() for
  // output-parameter discovery; a one-time getNode() snapshot would re-render
  // with stale data after useUpdate commits typed input.
  const nodeSlice = useNodesData<PrintPageNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "printPage") {
    return null;
  }
  return <PrintPageEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function PrintPageEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: PrintPageNodeData;
}) {
  const {
    editable,
    format,
    printBackground,
    includeTimestamp,
    customFilename,
    landscape,
    parameterKeys,
  } = data;

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const update = useUpdate<PrintPageNode["data"]>({ id: blockId, editable });

  return (
    <div data-testid="print-page-block-form" className="space-y-4">
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <Label className="text-xs text-slate-300">Page Format</Label>
          <Select
            value={format}
            onValueChange={(value) => update({ format: value })}
            disabled={!editable}
          >
            <SelectTrigger className="nopan w-36 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="A4">A4</SelectItem>
              <SelectItem value="Letter">Letter</SelectItem>
              <SelectItem value="Legal">Legal</SelectItem>
              <SelectItem value="Tabloid">Tabloid</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Print Background</Label>
            <HelpTooltip content="Include CSS background colors and images in the PDF" />
          </div>
          <Switch
            checked={printBackground}
            onCheckedChange={(checked) => update({ printBackground: checked })}
            disabled={!editable}
          />
        </div>
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs font-normal text-slate-300">
              Headers & Footers
            </Label>
            <HelpTooltip content="Adds date, title, URL, and page numbers to the PDF" />
          </div>
          <Switch
            checked={includeTimestamp}
            onCheckedChange={(checked) => update({ includeTimestamp: checked })}
            disabled={!editable}
          />
        </div>
      </div>
      <Separator />
      <Accordion type="single" collapsible defaultValue="advanced">
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <div className="space-y-4">
              <ParametersMultiSelect
                availableOutputParameters={outputParameterKeys}
                parameters={parameterKeys}
                onParametersChange={(next) => {
                  update({ parameterKeys: next });
                }}
              />
              <div className="space-y-2">
                <Label className="text-xs text-slate-300">
                  Custom Filename
                </Label>
                <Input
                  value={customFilename}
                  onChange={(e) => update({ customFilename: e.target.value })}
                  placeholder="my_report"
                  disabled={!editable}
                  className="nopan text-xs"
                />
              </div>
              <div className="flex items-center justify-between">
                <Label className="text-xs font-normal text-slate-300">
                  Landscape
                </Label>
                <Switch
                  checked={landscape}
                  onCheckedChange={(checked) => update({ landscape: checked })}
                  disabled={!editable}
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { PrintPageEditor };
