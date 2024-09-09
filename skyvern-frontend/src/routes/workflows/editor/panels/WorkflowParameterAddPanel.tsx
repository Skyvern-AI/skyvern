import { Cross2Icon } from "@radix-ui/react-icons";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { useState } from "react";
import { WorkflowParameterValueType } from "../../types/workflowTypes";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { ParametersState } from "../FlowRenderer";

type Props = {
  type: "workflow" | "credential";
  onClose: () => void;
  onSave: (value: ParametersState[number]) => void;
};

const workflowParameterTypeOptions = [
  { label: "string", value: WorkflowParameterValueType.String },
  { label: "number", value: WorkflowParameterValueType.Float },
  { label: "boolean", value: WorkflowParameterValueType.Boolean },
  { label: "file", value: WorkflowParameterValueType.FileURL },
  { label: "JSON", value: WorkflowParameterValueType.JSON },
];

function WorkflowParameterAddPanel({ type, onClose, onSave }: Props) {
  const [key, setKey] = useState("");
  const [urlParameterKey, setUrlParameterKey] = useState("");
  const [description, setDescription] = useState("");
  const [collectionId, setCollectionId] = useState("");
  const [parameterType, setParameterType] =
    useState<WorkflowParameterValueType>("string");

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <span>
          Add {type === "workflow" ? "Workflow" : "Credential"} Parameter
        </span>
        <Cross2Icon className="h-6 w-6 cursor-pointer" onClick={onClose} />
      </header>
      <div className="space-y-1">
        <Label className="text-xs text-slate-300">Key</Label>
        <Input value={key} onChange={(e) => setKey(e.target.value)} />
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-slate-300">Description</Label>
        <Input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>
      {type === "workflow" && (
        <div className="space-y-1">
          <Label className="text-xs">Value Type</Label>
          <Select
            value={parameterType}
            onValueChange={(value) =>
              setParameterType(value as WorkflowParameterValueType)
            }
          >
            <SelectTrigger className="w-full">
              <SelectValue placeholder="Select a type" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {workflowParameterTypeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </div>
      )}
      {type === "credential" && (
        <>
          <div className="space-y-1">
            <Label className="text-xs text-slate-300">URL Parameter Key</Label>
            <Input
              value={urlParameterKey}
              onChange={(e) => setUrlParameterKey(e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs text-slate-300">Collection ID</Label>
            <Input
              value={collectionId}
              onChange={(e) => setCollectionId(e.target.value)}
            />
          </div>
        </>
      )}
      <div className="flex justify-end">
        <Button
          onClick={() => {
            if (type === "workflow") {
              onSave({
                key,
                parameterType: "workflow",
                dataType: parameterType,
                description,
              });
            }
            if (type === "credential") {
              onSave({
                key,
                parameterType: "credential",
                collectionId,
                urlParameterKey,
                description,
              });
            }
          }}
        >
          Save
        </Button>
      </div>
    </div>
  );
}

export { WorkflowParameterAddPanel };
