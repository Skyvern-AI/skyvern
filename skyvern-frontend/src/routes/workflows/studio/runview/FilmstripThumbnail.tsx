import { ImageIcon } from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";
import { cn } from "@/util/utils";

export function FilmstripThumbnail({
  artifactId,
  alt,
}: {
  artifactId: string | null;
  alt: string;
}) {
  const credentialGetter = useCredentialGetter();
  const [imgLoaded, setImgLoaded] = useState(false);
  // Shares the ["artifact", id] cache key with ActionScreenshot, so the hero
  // and the strip thumbnail reuse a single fetch per action.
  const { data } = useQuery<ArtifactApiResponse>({
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

  // Some actions (e.g. terminate) never capture a screenshot — show a neutral
  // placeholder instead of an empty black box.
  if (!artifactId || data?.archived) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-slate-elevation3">
        <ImageIcon className="h-5 w-5 text-muted-foreground/40" />
      </div>
    );
  }

  // While the artifact resolves and the image paints, show a skeleton rather
  // than a black box; fade the screenshot in once it has actually loaded.
  return (
    <div className="relative h-full w-full bg-slate-elevation3">
      {data ? (
        <img
          src={getImageURL(data)}
          alt={alt}
          onLoad={() => setImgLoaded(true)}
          className={cn(
            "h-full w-full object-cover object-top transition-opacity",
            imgLoaded ? "opacity-100" : "opacity-0",
          )}
          loading="lazy"
        />
      ) : null}
      {!imgLoaded ? (
        <Skeleton className="absolute inset-0 rounded-none" />
      ) : null}
    </div>
  );
}
