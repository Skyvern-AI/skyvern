import { useId, useState } from "react";
import { CreateNewTaskForm } from "./CreateNewTaskForm";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SampleCase } from "../types";
import { getSampleForInitialFormValues } from "../data/sampleTaskData";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

function CreateNewTask() {
  const [selectedCase, setSelectedCase] = useState<SampleCase>("geico");
  const caseInputId = useId();

  return (
    <div className="flex flex-col gap-8 max-w-5xl mx-auto">
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
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-lg">Create a new task</CardTitle>
          <CardDescription>
            Fill out the form below to create a new task. You can select a
            sample from above to prefill the form with sample data.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <CreateNewTaskForm
            key={selectedCase}
            initialValues={getSampleForInitialFormValues(selectedCase)}
          />
        </CardContent>
      </Card>
    </div>
  );
}

export { CreateNewTask };
