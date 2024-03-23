import { Navigate, createBrowserRouter } from "react-router-dom";
import { RootLayout } from "./routes/root/RootLayout";
import { TasksPageLayout } from "./routes/tasks/TasksPageLayout";
import { CreateNewTask } from "./routes/tasks/CreateNewTask";
import { TaskList } from "./routes/tasks/TaskList";

const router = createBrowserRouter([
  {
    path: "/",
    element: <RootLayout />,
    children: [
      {
        index: true,
        element: <Navigate to="/tasks" />,
      },
      {
        path: "tasks",
        element: <TasksPageLayout />,
        children: [
          {
            index: true,
            element: <TaskList />,
          },
          {
            path: "new",
            element: <CreateNewTask />,
          },
        ],
      },
    ],
  },
]);

export { router };
