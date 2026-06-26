import { ReloadIcon } from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";

/**
 * The pinned/active action screenshot, letterboxed to fit the run hero. Shares the
 * ["artifact", id] cache with the filmstrip thumbnails so each is fetched once.
 */
export function HeroScreenshot({ artifactId }: { artifactId: string | null }) {
  const credentialGetter = useCredentialGetter();
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
  return (
    <img
      src={getImageURL(data)}
      alt="action screenshot"
      className="absolute inset-0 h-full w-full object-contain"
    />
  );
}
