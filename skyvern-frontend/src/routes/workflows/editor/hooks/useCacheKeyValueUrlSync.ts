import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";

/**
 * `ready` must be the same per-wpid init gate Workspace uses to call
 * useCacheKeyValueStore.initialize(...). Without it, an A->B nav reuses
 * Workspace, the store still holds A's value, and this hook would write
 * A's filter into B's URL before B's init lands.
 */
export function useCacheKeyValueUrlSync(ready: boolean): void {
  const [searchParams, setSearchParams] = useSearchParams();
  const cacheKeyValue = useCacheKeyValueStore((s) => s.cacheKeyValue);
  const isExplicit = useCacheKeyValueStore((s) => s.isExplicit);

  useEffect(() => {
    if (!ready) return;

    const currentUrlValue = searchParams.get("cache-key-value");

    if (!isExplicit) {
      if (currentUrlValue !== null) {
        setSearchParams(
          (prev) => {
            const newParams = new URLSearchParams(prev);
            newParams.delete("cache-key-value");
            return newParams;
          },
          { replace: true },
        );
      }
      return;
    }

    const targetValue = cacheKeyValue === "" ? null : cacheKeyValue;

    if (currentUrlValue !== targetValue) {
      setSearchParams(
        (prev) => {
          const newParams = new URLSearchParams(prev);
          if (cacheKeyValue === "") {
            newParams.delete("cache-key-value");
          } else {
            newParams.set("cache-key-value", cacheKeyValue);
          }
          return newParams;
        },
        { replace: true },
      );
    }
  }, [ready, cacheKeyValue, isExplicit, searchParams, setSearchParams]);
}
