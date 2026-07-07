import { useReactFlow } from "@xyflow/react";
import { useEffect, useMemo } from "react";

import {
  GOOGLE_SHEETS_REQUIRED_SCOPES,
  getDefaultGoogleOAuthCredentialId,
  hasGoogleOAuthCredentialScopes,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";

import type { AppNode } from "../nodes";
import { isGoogleSheetsReadNode } from "../nodes/GoogleSheetsReadNode/types";
import { isGoogleSheetsWriteNode } from "../nodes/GoogleSheetsWriteNode/types";

/**
 * Canvas-level fallback for the per-block GoogleOAuthCredentialSelector, which
 * only auto-fills the default account while its editor is mounted (build mode +
 * expanded). Collapsed or never-expanded blocks would otherwise reach save/run
 * with an empty credential_id and fail with "Google account is required".
 */
export function useResolveDefaultGoogleSheetsCredential(
  nodes: Array<AppNode>,
  readOnly: boolean = false,
): void {
  const { updateNodeData } = useReactFlow<AppNode>();
  const setHasChanges = useWorkflowHasChangesStore((s) => s.setHasChanges);

  const unconfiguredNodeIds = useMemo(
    () =>
      nodes
        .filter(
          (node) =>
            (isGoogleSheetsWriteNode(node) || isGoogleSheetsReadNode(node)) &&
            node.data.editable &&
            !node.data.credentialId.trim(),
        )
        .map((node) => node.id)
        .sort(),
    [nodes],
  );

  // Only fetch credentials when a block actually needs one, so workflows with no
  // (unconfigured) Sheets blocks and read-only canvases don't fetch on mount.
  // Wait out in-flight refetches (isFetching) so an invalidation — e.g. an
  // account disconnected in another tab — can't get filled from stale cache.
  const { credentials, isLoading, isFetching } = useGoogleOAuthCredentials({
    enabled: !readOnly && unconfiguredNodeIds.length > 0,
  });
  const googleSheetsCredentials = useMemo(
    () =>
      credentials.filter((credential) =>
        hasGoogleOAuthCredentialScopes(
          credential,
          GOOGLE_SHEETS_REQUIRED_SCOPES,
        ),
      ),
    [credentials],
  );
  const defaultCredentialId = getDefaultGoogleOAuthCredentialId(
    googleSheetsCredentials,
  );

  // Stable for a given set of unconfigured blocks regardless of node ordering.
  const unconfiguredKey = unconfiguredNodeIds.join(",");

  useEffect(() => {
    if (readOnly || isLoading || isFetching || !defaultCredentialId) {
      return;
    }
    if (unconfiguredNodeIds.length === 0) {
      return;
    }
    for (const id of unconfiguredNodeIds) {
      updateNodeData(id, { credentialId: defaultCredentialId });
    }
    // Filling a credential is a real, persistable edit, but this effect can run
    // in the same passive-effect flush as Workspace's mount initializer, which
    // runs later (child effects before parent) and calls setHasChanges(false).
    // Defer to a microtask so the dirty flag is set after that reset; otherwise
    // the fill is silently unsaved and a later Run reads the stale credential_id
    // from the backend.
    queueMicrotask(() => setHasChanges(true));
    // unconfiguredNodeIds is derived from unconfiguredKey; depending on the key
    // keeps the effect from re-firing on unrelated node-array identity changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readOnly, isLoading, isFetching, defaultCredentialId, unconfiguredKey]);
}
