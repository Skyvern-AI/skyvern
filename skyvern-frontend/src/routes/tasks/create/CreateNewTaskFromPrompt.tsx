import { useLocation } from "react-router-dom";
import { CreateNewTaskForm } from "./CreateNewTaskForm";
import { MagicWandIcon } from "@radix-ui/react-icons";

function CreateNewTaskFromPrompt() {
  const location = useLocation();

  const state = location.state.data;
  return (
    <section className="space-y-8">
      <header className="flex flex-col gap-4">
        <div className="flex gap-4 items-center">
          <MagicWandIcon className="w-6 h-6" />
          <h1 className="text-3xl font-bold">Create New Task</h1>
        </div>
        <p>
          Prompt: <span>{state.user_prompt}</span>
        </p>
        <p>
          Below are the parameters we generated automatically. You can go ahead
          and create the task if everything looks correct.
        </p>
      </header>
      <CreateNewTaskForm
        initialValues={{
          url: state.url,
          navigationGoal: state.navigation_goal,
          dataExtractionGoal: state.data_extraction_goal,
          extractedInformationSchema: JSON.stringify(
            state.extracted_information_schema,
            null,
            2,
          ),
          navigationPayload: JSON.stringify(state.navigation_payload, null, 2),
          webhookCallbackUrl: "",
        }}
      />
    </section>
  );
}

export { CreateNewTaskFromPrompt };
