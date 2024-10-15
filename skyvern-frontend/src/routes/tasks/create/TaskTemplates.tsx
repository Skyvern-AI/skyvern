import { PromptBox } from "./PromptBox";
import { SavedTasks } from "./SavedTasks";

function TaskTemplates() {
  return (
    <div className="space-y-8">
      <PromptBox />
      <h2 className="text-3xl">My Tasks</h2>
      <SavedTasks />
    </div>
  );
}

export { TaskTemplates };
