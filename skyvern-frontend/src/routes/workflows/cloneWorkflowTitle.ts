const WORKFLOW_TITLE_MAX_LENGTH = 255;

function getClonedWorkflowTitle(title: string): string {
  return [...`Copy of ${title}`].slice(0, WORKFLOW_TITLE_MAX_LENGTH).join("");
}

export { getClonedWorkflowTitle, WORKFLOW_TITLE_MAX_LENGTH };
