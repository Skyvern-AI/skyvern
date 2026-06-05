import type { NavigateFunction } from "react-router-dom";
import { DRAFT_WORKFLOW_PERMANENT_ID } from "./draftWorkflow";

type NavigateToBlankAgentOptions = {
  via?: string;
  folderId?: string | null;
};

function buildBlankAgentBuildPath({
  via,
  folderId,
}: NavigateToBlankAgentOptions = {}) {
  const params = new URLSearchParams();
  if (via) {
    params.set("via", via);
  }
  if (folderId) {
    params.set("folder_id", folderId);
  }
  const search = params.toString();
  return `/workflows/${DRAFT_WORKFLOW_PERMANENT_ID}/build${search ? `?${search}` : ""}`;
}

function navigateToBlankAgentEditor(
  navigate: NavigateFunction,
  options: NavigateToBlankAgentOptions = {},
) {
  navigate(buildBlankAgentBuildPath(options));
}

export {
  buildBlankAgentBuildPath,
  navigateToBlankAgentEditor,
  type NavigateToBlankAgentOptions,
};
