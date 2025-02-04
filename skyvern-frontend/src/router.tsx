import { Navigate, Outlet, createBrowserRouter } from "react-router-dom";
import { RootLayout } from "./routes/root/RootLayout";
import { Settings } from "./routes/settings/Settings";
import { SettingsPageLayout } from "./routes/settings/SettingsPageLayout";
import { TasksPageLayout } from "./routes/tasks/TasksPageLayout";
import { CreateNewTaskFormPage } from "./routes/tasks/create/CreateNewTaskFormPage";
import { RetryTask } from "./routes/tasks/create/retry/RetryTask";
import { StepArtifactsLayout } from "./routes/tasks/detail/StepArtifactsLayout";
import { TaskActions } from "./routes/tasks/detail/TaskActions";
import { TaskDetails } from "./routes/tasks/detail/TaskDetails";
import { TaskParameters } from "./routes/tasks/detail/TaskParameters";
import { TaskRecording } from "./routes/tasks/detail/TaskRecording";
import { TasksPage } from "./routes/tasks/list/TasksPage";
import { WorkflowPage } from "./routes/workflows/WorkflowPage";
import { WorkflowRun } from "./routes/workflows/WorkflowRun";
import { WorkflowRunParameters } from "./routes/workflows/WorkflowRunParameters";
import { Workflows } from "./routes/workflows/Workflows";
import { WorkflowsPageLayout } from "./routes/workflows/WorkflowsPageLayout";
import { WorkflowEditor } from "./routes/workflows/editor/WorkflowEditor";
import { WorkflowRunOutput } from "./routes/workflows/workflowRun/WorkflowRunOutput";
import { WorkflowPostRunParameters } from "./routes/workflows/workflowRun/WorkflowPostRunParameters";
import { WorkflowRunRecording } from "./routes/workflows/workflowRun/WorkflowRunRecording";
import { WorkflowRunOverview } from "./routes/workflows/workflowRun/WorkflowRunOverview";
import { DiscoverPageLayout } from "./routes/discover/DiscoverPageLayout";
import { DiscoverPage } from "./routes/discover/DiscoverPage";
import { HistoryPageLayout } from "./routes/history/HistoryPageLayout";
import { HistoryPage } from "./routes/history/HistoryPage";

const router = createBrowserRouter([
  {
    path: "/",
    element: <RootLayout />,
    children: [
      {
        index: true,
        element: <Navigate to="/discover" />,
      },
      {
        path: "tasks",
        element: <TasksPageLayout />,
        children: [
          {
            index: true,
            element: <TasksPage />,
          },
          {
            path: "create",
            element: <Outlet />,
            children: [
              {
                path: ":template",
                element: <CreateNewTaskFormPage />,
              },
              {
                path: "retry/:taskId",
                element: <RetryTask />,
              },
            ],
          },
          {
            path: ":taskId",
            element: <TaskDetails />,
            children: [
              {
                index: true,
                element: <Navigate to="actions" />,
              },
              {
                path: "actions",
                element: <TaskActions />,
              },
              {
                path: "recording",
                element: <TaskRecording />,
              },
              {
                path: "parameters",
                element: <TaskParameters />,
              },
              {
                path: "diagnostics",
                element: <StepArtifactsLayout />,
              },
            ],
          },
        ],
      },
      {
        path: "workflows",
        element: <WorkflowsPageLayout />,
        children: [
          {
            index: true,
            element: <Workflows />,
          },
          {
            path: ":workflowPermanentId",
            element: <Outlet />,
            children: [
              {
                index: true,
                element: <Navigate to="runs" />,
              },
              {
                path: "edit",
                element: <WorkflowEditor />,
              },
              {
                path: "run",
                element: <WorkflowRunParameters />,
              },
              {
                path: "runs",
                element: <WorkflowPage />,
              },
              {
                path: ":workflowRunId",
                element: <WorkflowRun />,
                children: [
                  {
                    index: true,
                    element: <Navigate to="overview" />,
                  },
                  {
                    path: "blocks",
                    element: <Navigate to="overview" />,
                  },
                  {
                    path: "overview",
                    element: <WorkflowRunOverview />,
                  },
                  {
                    path: "output",
                    element: <WorkflowRunOutput />,
                  },
                  {
                    path: "parameters",
                    element: <WorkflowPostRunParameters />,
                  },

                  {
                    path: "recording",
                    element: <WorkflowRunRecording />,
                  },
                ],
              },
            ],
          },
        ],
      },
      {
        path: "discover",
        element: <DiscoverPageLayout />,
        children: [
          {
            index: true,
            element: <DiscoverPage />,
          },
        ],
      },
      {
        path: "history",
        element: <HistoryPageLayout />,
        children: [
          {
            index: true,
            element: <HistoryPage />,
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
