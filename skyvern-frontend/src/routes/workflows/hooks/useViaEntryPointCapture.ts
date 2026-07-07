import { usePostHog } from "posthog-js/react";
import { useSearchParams } from "react-router-dom";

import { useMountEffect } from "@/hooks/useMountEffect";

// Fires once on mount when a `?via=` entry point is present. Lives in a hook so
// every editor surface a handoff can land on reports it — the studio
// WorkflowEditor and the studio-off /build Debugger — not just the studio path.
export function useViaEntryPointCapture(): void {
  const [searchParams] = useSearchParams();
  const posthog = usePostHog();
  useMountEffect(() => {
    const via = searchParams.get("via");
    if (via) {
      posthog?.capture("copilot.discover.started", { entry_point: via });
    }
  });
}
