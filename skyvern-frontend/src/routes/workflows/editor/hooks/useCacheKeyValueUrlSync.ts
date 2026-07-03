import { useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { toReadableSearch } from "@/routes/workflows/studio/panes";
import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";

/**
 * `ready` must be the same per-wpid init gate Workspace uses to call
 * useCacheKeyValueStore.initialize(...). Without it, an A->B nav reuses
 * Workspace, the store still holds A's value, and this hook would write
 * A's filter into B's URL before B's init lands.
 */
export function useCacheKeyValueUrlSync(ready: boolean): void {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const cacheKeyValue = useCacheKeyValueStore((s) => s.cacheKeyValue);
  const isExplicit = useCacheKeyValueStore((s) => s.isExplicit);

  useEffect(() => {
    if (!ready) return;

    const currentUrlValue = searchParams.get("cache-key-value");

    if (!isExplicit) {
      if (currentUrlValue !== null) {
        const newParams = new URLSearchParams(searchParams);
        newParams.delete("cache-key-value");
        navigate({ search: toReadableSearch(newParams) }, { replace: true });
      }
      return;
    }

    const targetValue = cacheKeyValue === "" ? null : cacheKeyValue;

    if (currentUrlValue !== targetValue) {
      const newParams = new URLSearchParams(searchParams);
      if (cacheKeyValue === "") {
        newParams.delete("cache-key-value");
      } else {
        newParams.set("cache-key-value", cacheKeyValue);
      }
      navigate({ search: toReadableSearch(newParams) }, { replace: true });
    }
  }, [ready, cacheKeyValue, isExplicit, searchParams, navigate]);
}
