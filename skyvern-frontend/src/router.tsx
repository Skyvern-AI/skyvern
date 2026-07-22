import { Navigate, Outlet, createBrowserRouter } from "react-router-dom";
import {
  BuildRoute,
  DebugRoute,
  EditRoute,
  StudioRoute,
} from "@/routes/workflows/StudioRouteGates";
import { LegacyWorkflowsRedirect } from "@/routes/workflows/LegacyWorkflowsRedirect";
import { BrowserSession } from "@/routes/browserSessions/BrowserSession";
import { BrowserSessions } from "@/routes/browserSessions/BrowserSessions";
import { PageLayout } from "./components/PageLayout";
import { DiscoverPage } from "./routes/discover/DiscoverPage";
import { HistoryPage } from "./routes/history/HistoryPage";
import { RootLayout } from "./routes/root/RootLayout";
import { Settings } from "./routes/settings/Settings";
import { LabelManagement } from "./routes/settings/LabelManagement";
import { CreateNewTaskFormPage } from "./routes/tasks/create/CreateNewTaskFormPage";
import { RetryTask } from "./routes/tasks/create/retry/RetryTask";
import { StepArtifactsLayout } from "./routes/tasks/detail/StepArtifactsLayout";
import { TaskActions } from "./routes/tasks/detail/TaskActions";
import { TaskDetails } from "./routes/tasks/detail/TaskDetails";
import { TaskParameters } from "./routes/tasks/detail/TaskParameters";
import { TaskRecording } from "./routes/tasks/detail/TaskRecording";
import { TasksPage } from "./routes/tasks/list/TasksPage";
import { WorkflowPage } from "./routes/workflows/WorkflowPage";
import { WorkflowScriptDetailPage } from "./routes/workflows/WorkflowScriptDetailPage";
import { WorkflowScriptsPage } from "./routes/workflows/WorkflowScriptsPage";
import { WorkflowRun } from "./routes/workflows/WorkflowRun";
import { WorkflowRunParameters } from "./routes/workflows/WorkflowRunParameters";
import { Workflows } from "./routes/workflows/Workflows";
import { WorkflowsPageLayout } from "./routes/workflows/WorkflowsPageLayout";
import { WorkflowPostRunParameters } from "./routes/workflows/workflowRun/WorkflowPostRunParameters";
import { WorkflowRunOutput } from "./routes/workflows/workflowRun/WorkflowRunOutput";
import { WorkflowRunOverview } from "./routes/workflows/workflowRun/WorkflowRunOverview";
import { WorkflowRunRecording } from "./routes/workflows/workflowRun/WorkflowRunRecording";
import { WorkflowRunCode } from "@/routes/workflows/workflowRun/WorkflowRunCode";
import { DebugStoreProvider } from "@/store/DebugStoreContext";
import { BrowserProfileDetailPage } from "@/routes/browserProfiles/BrowserProfileDetailPage.tsx";
import { BrowserProfilesPage } from "@/routes/browserProfiles/BrowserProfilesPage.tsx";
import { CredentialsPage } from "@/routes/credentials/CredentialsPage.tsx";
import { GoogleOAuthCallback } from "@/routes/integrations/GoogleOAuthCallback";
import { Integrations } from "@/routes/integrations/Integrations";
import { RecipeComingSoonPage } from "@/routes/recipes/RecipeComingSoonPage";
import { RecipesPage } from "@/routes/recipes/RecipesPage";
import { RunRouter } from "@/routes/runs/RunRouter";
import { SchedulesRoute } from "@/routes/schedules/SchedulesRoute";
import { ScheduleDetailRoute } from "@/routes/schedules/ScheduleDetailRoute";

const recipeComingSoonRoutes = [
  {
    path: "recipes/invoices",
    title: "Invoices",
    description:
      "Skyvern's Invoices Agent allows you to automate invoice collection and downloads with agents",
  },
  {
    path: "recipes/government",
    title: "Government",
    description:
      "Skyvern's Government Agent allows you to navigate any government websites",
  },
  {
    path: "recipes/healthcare",
    title: "Healthcare",
    description:
      "Skyvern's Healthcare Agent allows you to automate work with healthcare websites",
  },
  {
    path: "recipes/insurance",
    title: "Insurance",
    description:
      "Skyvern's Insurance Agent allows you to automate work with insurance websites",
  },
  {
    path: "recipes/purchasing",
    title: "Purchasing",
    description:
      "Skyvern's Purchasing Agent allows you to make payments on the web",
  },
  {
    path: "recipes/crm",
    title: "CRM",
    description: "Skyvern's CRM Agent allows you to navigate any CRM",
  },
  {
    path: "recipes/logistics",
    title: "Logistics",
    description:
      "Skyvern's Logistics Agent allows you to automate work with logistics websites",
  },
  {
    path: "recipes/contact-forms",
    title: "Contact Forms",
    description:
      "Skyvern's Contact Forms Agent allows you to submit contact forms across websites",
  },
  {
    path: "recipes/job-apps",
    title: "Job Apps",
    description:
      "Skyvern's Job Apps Agent allows you to automate job applications with agents",
  },
].map(({ path, title, description }) => ({
  path,
  element: <PageLayout />,
  children: [
    {
      index: true,
      element: <RecipeComingSoonPage title={title} description={description} />,
    },
  ],
}));

const router = createBrowserRouter([
  {
    path: "browser-session/:browserSessionId",
    element: <BrowserSession />,
    children: [
      { index: true, element: <Navigate to="stream" replace /> },
      { path: "stream", element: <></> },
      { path: "recordings", element: <></> },
      { path: "downloads", element: <></> },
      { path: "timeline", element: <></> },
      { path: "runs", element: <></> },
    ],
  },
  {
    path: "/",
    element: (
      <DebugStoreProvider>
        <RootLayout />
      </DebugStoreProvider>
    ),
    children: [
      {
        path: "runs",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <HistoryPage />,
          },
        ],
      },
      {
        path: "runs/:runId/*",
        element: <RunRouter />,
      },
      {
        path: "schedules",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <SchedulesRoute />,
          },
          {
            path: ":workflowPermanentId/:scheduleId",
            element: <ScheduleDetailRoute />,
          },
        ],
      },
      {
        path: "browser-sessions",
        element: <BrowserSessions />,
      },
      {
        index: true,
        element: <Navigate to="/discover" />,
      },
      {
        path: "tasks",
        element: <PageLayout />,
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
        path: "workflows/*",
        element: <LegacyWorkflowsRedirect />,
      },
      {
        path: "agents",
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
                path: "build",
                element: <BuildRoute />,
              },
              {
                path: ":workflowRunId/:blockLabel/build",
                element: <BuildRoute />,
              },
              {
                path: "debug",
                element: <DebugRoute />,
              },
              {
                path: ":workflowRunId/:blockLabel/debug",
                element: <DebugRoute />,
              },
              {
                path: "edit",
                element: <EditRoute />,
              },
              {
                path: "studio",
                element: <StudioRoute />,
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
                path: "scripts",
                element: <Outlet />,
                children: [
                  {
                    index: true,
                    element: <WorkflowScriptsPage />,
                  },
                  {
                    path: ":scriptId",
                    element: <WorkflowScriptDetailPage />,
                  },
                ],
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
                  {
                    path: "code",
                    element: (
                      <WorkflowRunCode showCacheKeyValueSelector={true} />
                    ),
                  },
                ],
              },
            ],
          },
        ],
      },
      {
        path: "discover",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <DiscoverPage />,
          },
        ],
      },
      {
        path: "recipes",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <RecipesPage />,
          },
        ],
      },
      ...recipeComingSoonRoutes,
      {
        path: "history",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <HistoryPage />,
          },
        ],
      },
      {
        path: "settings",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <Settings />,
          },
          {
            path: "labels",
            element: <LabelManagement />,
          },
        ],
      },
      {
        path: "credentials",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <CredentialsPage />,
          },
        ],
      },
      {
        path: "browser-profiles",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <BrowserProfilesPage />,
          },
          {
            path: ":profileId",
            element: <BrowserProfileDetailPage />,
          },
        ],
      },
      {
        path: "integrations",
        element: <PageLayout />,
        children: [
          {
            index: true,
            element: <Integrations />,
          },
          {
            path: "google/callback",
            element: <GoogleOAuthCallback />,
          },
          {
            path: "microsoft/callback",
            element: <Navigate to="/integrations" replace />,
          },
        ],
      },
    ],
  },
]);

export { router };
