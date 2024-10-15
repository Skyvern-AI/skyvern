import { useNavigate } from "react-router-dom";
import { getSample } from "../data/sampleTaskData";
import { SampleCase } from "../types";
import { PromptBox } from "./PromptBox";
import { SavedTasks } from "./SavedTasks";
import { SwitchBar } from "@/components/SwitchBar";
import { useState } from "react";
import { TaskTemplateCard } from "./TaskTemplateCard";

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
  job_application: {
    title: "Job Application",
    description: "Fill a job application form",
  },
};

const templateSwitchOptions = [
  {
    label: "Skyvern Templates",
    value: "skyvern",
  },
  {
    label: "My Templates",
    value: "user",
  },
];

function TaskTemplates() {
  const navigate = useNavigate();
  const [templateSwitchValue, setTemplateSwitchValue] =
    useState<(typeof templateSwitchOptions)[number]["value"]>("skyvern");

  return (
    <div className="space-y-8">
      <PromptBox />
      <section>
        <SwitchBar
          value={templateSwitchValue}
          onChange={setTemplateSwitchValue}
          options={templateSwitchOptions}
        />
      </section>
      <section>
        {templateSwitchValue === "skyvern" ? (
          <div className="grid grid-cols-4 gap-4">
            {Object.entries(templateSamples).map(([sampleKey, sample]) => {
              return (
                <TaskTemplateCard
                  key={sampleKey}
                  title={sample.title}
                  description={getSample(sampleKey as SampleCase).url}
                  body={sample.description}
                  onClick={() => {
                    navigate(`/create/${sampleKey}`);
                  }}
                />
              );
            })}
          </div>
        ) : (
          <SavedTasks />
        )}
      </section>
    </div>
  );
}

export { TaskTemplates };
