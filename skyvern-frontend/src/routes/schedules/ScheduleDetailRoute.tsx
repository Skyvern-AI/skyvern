import { Navigate } from "react-router-dom";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { ScheduleDetailPage } from "@/routes/schedules/ScheduleDetailPage";

export function ScheduleDetailRoute() {
  const enabled = useFeatureFlag("WORKFLOW_SCHEDULES");
  if (enabled) return <ScheduleDetailPage />;
  return <Navigate to="/workflows" replace />;
}
