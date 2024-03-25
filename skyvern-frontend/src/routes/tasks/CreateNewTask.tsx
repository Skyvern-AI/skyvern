import { useId, useState } from "react";
import { CreateNewTaskForm } from "./CreateNewTaskForm";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SampleCase } from "./types";
import { getSampleForInitialFormValues } from "./sampleTaskData";
import { Label } from "@/components/ui/label";

function CreateNewTask() {
  const [selectedCase, setSelectedCase] = useState<SampleCase>("geico");
  const caseInputId = useId();

  return (
    <div className="flex flex-col gap-6 px-6">
      <div className="flex gap-4 items-center">
        <Label htmlFor={caseInputId} className="whitespace-nowrap">
          Select a sample:
        </Label>
        <Select
          value={selectedCase}
          onValueChange={(value) => {
            setSelectedCase(value as SampleCase);
          }}
        >
          <SelectTrigger>
            <SelectValue placeholder="Select a case" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="geico">Geico</SelectItem>
            <SelectItem value="finditparts">Finditparts</SelectItem>
            <SelectItem value="california_edd">California_EDD</SelectItem>
            <SelectItem value="bci_seguros">bci_seguros</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <CreateNewTaskForm
        key={selectedCase}
        initialValues={getSampleForInitialFormValues(selectedCase)}
      />
    </div>
  );
}

export { CreateNewTask };
