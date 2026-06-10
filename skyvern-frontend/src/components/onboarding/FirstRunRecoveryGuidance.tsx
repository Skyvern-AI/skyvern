import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  RecoveryGuidanceTelemetry,
  type Surface,
} from "@/util/onboarding/recoveryGuidanceTelemetry";
import {
  getRecoveryPaths,
  type RecoveryPath,
  type RecoveryPathId,
} from "./recoveryPaths";

const DOCS_URL = "https://docs.skyvern.com";
const SUPPORT_URL = "mailto:support@skyvern.com";

type Props = Readonly<{
  surface: Surface;
  failureCategory: string | null;
  workflowPermanentId?: string | null;
  onRetry?: () => Promise<void> | void;
}>;

function FirstRunRecoveryGuidance({
  surface,
  failureCategory,
  workflowPermanentId,
  onRetry,
}: Props) {
  const navigate = useNavigate();
  const paths = getRecoveryPaths(failureCategory);
  const shownRef = useRef(false);

  useEffect(() => {
    if (shownRef.current) {
      return;
    }
    shownRef.current = true;
    RecoveryGuidanceTelemetry.recoveryGuidanceShown(
      surface,
      failureCategory,
      paths.length,
    );
  }, [surface, failureCategory, paths.length]);

  function externalUrlFor(id: RecoveryPathId): string {
    return id === "contact_support" ? SUPPORT_URL : DOCS_URL;
  }

  function routeFor(id: RecoveryPathId): string {
    if (id === "edit_workflow") {
      return workflowPermanentId
        ? `/workflows/${workflowPermanentId}/build`
        : "/workflows";
    }
    return "/credentials";
  }

  async function handlePathClick(path: RecoveryPath): Promise<void> {
    RecoveryGuidanceTelemetry.recoveryPathChosen(
      surface,
      failureCategory,
      path.id,
    );

    if (path.kind === "retry") {
      try {
        await onRetry?.();
        RecoveryGuidanceTelemetry.recoveryOutcome(
          surface,
          failureCategory,
          path.id,
          "retry_started",
        );
      } catch {
        RecoveryGuidanceTelemetry.recoveryOutcome(
          surface,
          failureCategory,
          path.id,
          "retry_failed_to_start",
        );
      }
      return;
    }

    if (path.kind === "external") {
      const url = externalUrlFor(path.id);
      // Capture before navigating: a mailto hand-off can pause JS and drop a
      // post-navigation capture.
      RecoveryGuidanceTelemetry.recoveryOutcome(
        surface,
        failureCategory,
        path.id,
        "opened",
      );
      if (url.startsWith("mailto:")) {
        window.location.href = url;
      } else {
        window.open(url, "_blank", "noopener,noreferrer");
      }
      return;
    }

    navigate(routeFor(path.id));
    RecoveryGuidanceTelemetry.recoveryOutcome(
      surface,
      failureCategory,
      path.id,
      "navigated",
    );
  }

  return (
    <div
      data-testid="first-run-recovery-guidance"
      className="space-y-2 border-t border-red-600/40 pt-3"
    >
      <div className="text-sm font-medium">
        Not sure what to do next? Try one of these:
      </div>
      <div className="flex flex-wrap gap-2">
        {paths.map((path) => (
          <Button
            key={path.id}
            size="sm"
            variant="secondary"
            data-testid={`recovery-path-${path.id}`}
            onClick={() => {
              void handlePathClick(path);
            }}
          >
            {path.label}
          </Button>
        ))}
      </div>
    </div>
  );
}

export { FirstRunRecoveryGuidance };
