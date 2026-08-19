import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  RecoveryGuidanceTelemetry,
  type RecoveryGuidanceTelemetryContext,
} from "@/util/onboarding/recoveryGuidanceTelemetry";
import type { RecoveryGuidanceAssignment } from "@/store/onboarding/types";
import {
  getRecoveryPaths,
  type RecoveryPath,
  type RecoveryPathId,
} from "./recoveryPaths";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { workflowEditorPath } from "@/routes/workflows/studioNavigation";

const DOCS_URL = "https://docs.skyvern.com";
const SUPPORT_URL = "mailto:support@skyvern.com";

type Props = Readonly<{
  telemetryContext: RecoveryGuidanceTelemetryContext;
  workflowPermanentId?: string | null;
  onRetry?: () => void;
}>;

function shouldShowRecoveryGuidance({
  assignment,
  workflowRunId,
  treatmentSurfaceEnabled,
}: {
  assignment: RecoveryGuidanceAssignment | null;
  workflowRunId: string | undefined;
  treatmentSurfaceEnabled: boolean;
}): boolean {
  return (
    treatmentSurfaceEnabled &&
    assignment?.arm === "treatment" &&
    assignment.eligible_run_id === workflowRunId
  );
}

function FirstRunRecoveryGuidance({
  telemetryContext,
  workflowPermanentId,
  onRetry,
}: Props) {
  const navigate = useNavigate();
  const studioEnabled = useWorkflowStudioEnabled();
  const paths = getRecoveryPaths(telemetryContext.failureCategory);
  const shownRef = useRef(false);

  useEffect(() => {
    if (shownRef.current) {
      return;
    }
    shownRef.current = true;
    RecoveryGuidanceTelemetry.recoveryGuidanceShown(telemetryContext);
  }, [telemetryContext]);

  function routeFor(id: RecoveryPathId): string {
    if (id === "edit_workflow") {
      return workflowPermanentId
        ? workflowEditorPath(workflowPermanentId, studioEnabled)
        : "/agents";
    }
    return "/credentials";
  }

  function handlePathClick(path: RecoveryPath): void {
    RecoveryGuidanceTelemetry.recoveryGuidanceClicked(
      telemetryContext,
      path.id,
    );

    if (path.kind === "retry") {
      onRetry?.();
      return;
    }

    if (path.kind === "external") {
      window.open(
        path.id === "contact_support" ? SUPPORT_URL : DOCS_URL,
        "_blank",
        "noopener,noreferrer",
      );
      return;
    }

    navigate(routeFor(path.id));
  }

  return (
    <div
      data-testid="first-run-recovery-guidance"
      className="mt-3 rounded-md border border-amber-300 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950/30"
    >
      <div className="text-sm font-medium text-amber-900 dark:text-amber-100">
        Need help recovering from this run?
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        {paths.map((path) => (
          <Button
            key={path.id}
            data-testid={`recovery-path-${path.id}`}
            variant="outline"
            size="sm"
            onClick={() => handlePathClick(path)}
          >
            {path.label}
          </Button>
        ))}
      </div>
    </div>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export { FirstRunRecoveryGuidance, shouldShowRecoveryGuidance };
