import { Navigate, createBrowserRouter } from "react-router-dom";
import { RootLayout } from "./routes/root/RootLayout";
import { TasksPageLayout } from "./routes/tasks/TasksPageLayout";
import { TaskTemplates } from "./routes/tasks/create/TaskTemplates";
import { TaskList } from "./routes/tasks/list/TaskList";
import { Settings } from "./routes/settings/Settings";
import { SettingsPageLayout } from "./routes/settings/SettingsPageLayout";
import { TaskDetails } from "./routes/tasks/detail/TaskDetails";
import { CreateNewTaskLayout } from "./routes/tasks/create/CreateNewTaskLayout";
import { CreateNewTaskFormPage } from "./routes/tasks/create/CreateNewTaskFormPage";

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
            path: ":taskId",
            element: <TaskDetails />,
          },
        ],
      },
      {
        path: "create",
        element: <CreateNewTaskLayout />,
        children: [
          {
            index: true,
            element: <TaskTemplates />,
          },
          {
            path: ":template",
            element: <CreateNewTaskFormPage />,
          },
        ],
      },
      {
        path: "settings",
        element: <SettingsPageLayout />,
        children: [
          {
            index: true,
            element: <Settings />,
          },
        ],
      },
    ],
  },
]);

export { router };
