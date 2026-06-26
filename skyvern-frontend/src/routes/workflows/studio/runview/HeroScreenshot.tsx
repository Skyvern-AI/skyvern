import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";

import { screenshotZoomClasses } from "./HeroScreenshot.utils";

/**
 * The pinned/active action screenshot, fit to the run-hero width and scrollable
 * for long captures. Shares the ["artifact", id] cache with the filmstrip
 * thumbnails so each is fetched once.
 */
export function HeroScreenshot({ artifactId }: { artifactId: string | null }) {
  const credentialGetter = useCredentialGetter();
  const [zoomed, setZoomed] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setZoomed(false);
  }, [artifactId]);

  // Start each view at the top; when zoomed, center horizontally too (margin-auto
  // resolves to 0 once the image overflows, so the scroll would default to left).
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) {
      return;
    }
    el.scrollTop = 0;
    el.scrollLeft = zoomed ? (el.scrollWidth - el.clientWidth) / 2 : 0;
  }, [zoomed, artifactId]);
  const { data, isLoading } = useQuery<ArtifactApiResponse>({
    queryKey: ["artifact", artifactId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get(`/artifacts/${artifactId}`)
        .then((response) => response.data);
    },
    enabled: Boolean(artifactId),
    refetchOnWindowFocus: false,
    refetchOnMount: false,
    staleTime: Infinity,
    retry: 1,
  });

  if (!artifactId) {
    return (
      <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
        No screenshot for this action.
      </div>
    );
  }
  if (isLoading) {
    return (
      <div className="absolute inset-0 flex items-center justify-center gap-2 text-sm text-muted-foreground">
        <ReloadIcon className="h-5 w-5 animate-spin" /> Loading screenshot…
      </div>
    );
  }
  if (!data || data.archived) {
    return (
      <div className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
        Screenshot unavailable.
      </div>
    );
  }
  const toggleZoom = () => setZoomed((z) => !z);
  const zoom = screenshotZoomClasses(zoomed);
  return (
    <div
      ref={containerRef}
      className={zoom.container}
      role="button"
      tabIndex={0}
      aria-label={zoomed ? "Zoom screenshot out" : "Zoom screenshot in"}
      onClick={toggleZoom}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleZoom();
        }
      }}
    >
      <img
        src={getImageURL(data)}
        alt="action screenshot"
        className={zoom.image}
      />
    </div>
  );
}
