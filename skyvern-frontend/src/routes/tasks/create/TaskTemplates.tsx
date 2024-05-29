import { SampleCase } from "../types";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { useNavigate } from "react-router-dom";
import { SavedTasks } from "./SavedTasks";
import { getSample } from "../data/sampleTaskData";

const templateSamples: {
  [key in SampleCase]: {
    title: string;
    description: string;
  };
} = {
  blank: {
    title: "Blank",
    description: "Create task from a blank template",
  },
  geico: {
    title: "Geico",
    description: "Generate an auto insurance quote",
  },
  finditparts: {
    title: "Finditparts",
    description: "Find a product and add it to cart",
  },
  california_edd: {
    title: "California_EDD",
    description: "Fill the employer services online enrollment form",
  },
  bci_seguros: {
    title: "bci_seguros",
    description: "Generate an auto insurance quote",
  },
};

function TaskTemplates() {
  const navigate = useNavigate();

  return (
    <div>
      <section className="py-4">
        <header>
          <h1 className="text-3xl">Skyvern Templates</h1>
        </header>
        <Separator className="mt-2 mb-8" />
        <div className="grid grid-cols-4 gap-4">
          {Object.entries(templateSamples).map(([sampleKey, sample]) => {
            return (
              <Card key={sampleKey}>
                <CardHeader>
                  <CardTitle>{sample.title}</CardTitle>
                  <CardDescription className="overflow-hidden text-ellipsis whitespace-nowrap">
                    {getSample(sampleKey as SampleCase).url}
                  </CardDescription>
                </CardHeader>
                <CardContent
                  className="h-48 hover:bg-muted/40 cursor-pointer"
                  onClick={() => {
                    navigate(sampleKey);
                  }}
                >
                  {sample.description}
                </CardContent>
              </Card>
            );
          })}
        </div>
      </section>
      <section className="py-4">
        <header>
          <h1 className="text-3xl">Your Templates</h1>
        </header>
        <Separator className="mt-2 mb-8" />
        <SavedTasks />
      </section>
    </div>
  );
}

export { TaskTemplates };
