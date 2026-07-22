import { createContext, useContext } from "react";
import { useParams } from "react-router-dom";

export const WorkflowPermanentIdContext = createContext<string | undefined>(
  undefined,
);

/**
 * The workflow permanent id for the current studio/editor surface. Routes that
 * carry it in the path (/agents/:workflowPermanentId/...) resolve it from the
 * URL; the short run route (/runs/:runId) has no wpid segment, so its resolver
 * supplies it through the context. The context wins; the path param is the
 * fallback, so path-routed surfaces keep behaving exactly as before.
 */
export function useWorkflowPermanentId(): string | undefined {
  const override = useContext(WorkflowPermanentIdContext);
  const { workflowPermanentId } = useParams();
  return override ?? workflowPermanentId;
}
