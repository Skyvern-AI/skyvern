import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { QueuedTasks } from "../running/QueuedTasks";
import { RunningTasks } from "../running/RunningTasks";
import { TaskHistory } from "./TaskHistory";

function TaskList() {
  return (
    <div className="flex flex-col gap-8">
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-xl">Running Tasks</CardTitle>
          <CardDescription>Tasks that are currently running</CardDescription>
        </CardHeader>
        <CardContent className="p-4">
          <div className="grid grid-cols-4 gap-4">
            <RunningTasks />
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-xl">Queued Tasks</CardTitle>
          <CardDescription>Tasks that are waiting to run</CardDescription>
        </CardHeader>
        <CardContent className="p-4">
          <QueuedTasks />
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-xl">Task History</CardTitle>
          <CardDescription>Tasks you have run previously</CardDescription>
        </CardHeader>
        <CardContent className="p-4">
          <TaskHistory />
        </CardContent>
      </Card>
    </div>
  );
}

export { TaskList };
