import { useMatch, useSearchParams } from "react-router-dom";

type Options = {
  hideBrowserSessions?: boolean;
};

function useSidebarHidden({ hideBrowserSessions = false }: Options = {}) {
  const [searchParams] = useSearchParams();
  const embed = searchParams.get("embed");
  const workflowEditMatch = useMatch("/workflows/:workflowPermanentId/edit");
  const workflowBuildMatch = useMatch("/workflows/:workflowPermanentId/build");
  const workflowBlockBuildMatch = useMatch(
    "/workflows/:workflowPermanentId/:workflowRunId/:blockLabel/build",
  );
  const workflowDebugMatch = useMatch("/workflows/:workflowPermanentId/debug");
  const workflowBlockDebugMatch = useMatch(
    "/workflows/:workflowPermanentId/:workflowRunId/:blockLabel/debug",
  );
  const browserSessionMatch = useMatch("/browser-session/:browserSessionId");
  const nestedBrowserSessionMatch = useMatch(
    "/browser-session/:browserSessionId/*",
  );

  return Boolean(
    workflowEditMatch ||
    workflowBuildMatch ||
    workflowBlockBuildMatch ||
    workflowDebugMatch ||
    workflowBlockDebugMatch ||
    (hideBrowserSessions &&
      (browserSessionMatch || nestedBrowserSessionMatch)) ||
    embed === "true",
  );
}

export { useSidebarHidden };
