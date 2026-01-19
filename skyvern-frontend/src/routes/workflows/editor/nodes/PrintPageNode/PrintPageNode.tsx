import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Handle, NodeProps, Position, useNodes, useEdges } from "@xyflow/react";
import type { PrintPageNode } from "./types";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Separator } from "@/components/ui/separator";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";

function PrintPageNode({ id, data }: NodeProps<PrintPageNode>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;

  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);
  const update = useUpdate<PrintPageNode["data"]>({ id, editable });

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
          "w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
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
          type="print_page"
        />
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <Label className="text-xs text-slate-300">Page Format</Label>
            <Select
              value={data.format}
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
              checked={data.printBackground}
              onCheckedChange={(checked) =>
                update({ printBackground: checked })
              }
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
              checked={data.includeTimestamp}
              onCheckedChange={(checked) =>
                update({ includeTimestamp: checked })
              }
              disabled={!editable}
            />
          </div>
        </div>
        <Separator />
        <Accordion type="single" collapsible>
          <AccordionItem value="advanced" className="border-b-0">
            <AccordionTrigger className="py-0">
              Advanced Settings
            </AccordionTrigger>
            <AccordionContent className="pl-6 pr-1 pt-1">
              <div className="space-y-4">
                <ParametersMultiSelect
                  availableOutputParameters={outputParameterKeys}
                  parameters={data.parameterKeys}
                  onParametersChange={(parameterKeys) => {
                    update({ parameterKeys });
                  }}
                />
                <div className="space-y-2">
                  <Label className="text-xs text-slate-300">
                    Custom Filename
                  </Label>
                  <Input
                    value={data.customFilename}
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
                    checked={data.landscape}
                    onCheckedChange={(checked) =>
                      update({ landscape: checked })
                    }
                    disabled={!editable}
                  />
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </div>
  );
}

export { PrintPageNode };
