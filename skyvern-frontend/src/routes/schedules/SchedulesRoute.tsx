import { Navigate } from "react-router-dom";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { SchedulesPage } from "@/routes/schedules/SchedulesPage";

export function SchedulesRoute() {
  const enabled = useFeatureFlag("WORKFLOW_SCHEDULES");
  if (enabled) return <SchedulesPage />;
  return <Navigate to="/workflows" replace />;
}
