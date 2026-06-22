import { createContext, useContext } from "react";

type ActiveOrgId = string | undefined;
type OrgScopedQueryKey<TQueryKey extends ReadonlyArray<unknown>> =
  | TQueryKey
  | [...TQueryKey, string];
const UNSCOPED_QUERY_KEY_SCOPE = Symbol("unscoped query key scope");
type OrgScopedQueryKeyScope = string | typeof UNSCOPED_QUERY_KEY_SCOPE;

const ActiveOrgContext = createContext<ActiveOrgId>(undefined);

function useActiveOrgId(): ActiveOrgId {
  return useContext(ActiveOrgContext);
}

function getActiveOrgQueryKeyScope(
  activeOrgId: ActiveOrgId,
): OrgScopedQueryKeyScope {
  return activeOrgId ?? UNSCOPED_QUERY_KEY_SCOPE;
}

function getOrgScopedQueryKey<TQueryKey extends ReadonlyArray<unknown>>(
  queryKey: TQueryKey,
  activeOrgScope: OrgScopedQueryKeyScope,
): OrgScopedQueryKey<TQueryKey> {
  if (activeOrgScope === UNSCOPED_QUERY_KEY_SCOPE) {
    return queryKey;
  }
  return [...queryKey, activeOrgScope];
}

export {
  ActiveOrgContext,
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
};
export type { ActiveOrgId, OrgScopedQueryKeyScope };
