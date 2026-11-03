import { getClient } from "@/api/AxiosClient";
import {
  FeedbackRating,
  RunFeedbackApiResponse,
  RunFeedbackTargetType,
} from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useUser } from "@/hooks/useUser";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FeedbackRateOptions, FeedbackThumbs } from "./FeedbackThumbs";

type Props = {
  targetType: RunFeedbackTargetType;
  targetId: string;
  // "report" is for runs that already failed: one action, no thumbs up.
  variant?: "thumbs" | "report";
  className?: string;
};

type SubmitBody = {
  rating: FeedbackRating | null;
  reason?: string;
  needsSupport?: boolean;
};

/**
 * Thumbs for a finished run. The feedback route lives on base_router (/v1),
 * so requests go through the sans-api-v1 client like the copilot routes. Loads the saved rating so a reload shows the
 * user's earlier choice; every tap round-trips through POST /feedback.
 */
function RunFeedback({
  targetType,
  targetId,
  variant = "thumbs",
  className,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { get: getUser } = useUser();
  const queryKey = ["runFeedback", targetType, targetId];

  const { data: saved, isPending } = useQuery<RunFeedbackApiResponse | null>({
    queryKey,
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .get("/feedback", {
          params: { target_type: targetType, target_id: targetId },
        })
        .then((response) => response.data ?? null);
    },
  });

  const mutation = useMutation({
    mutationFn: async (body: SubmitBody) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .post("/feedback", {
          target_type: targetType,
          target_id: targetId,
          rating: body.rating,
          reason: body.reason || null,
          needs_support: body.needsSupport ?? false,
          submitted_by: getUser()?.email ?? null,
        })
        .then(
          (response) =>
            (response.data ?? null) as RunFeedbackApiResponse | null,
        );
    },
    onSuccess: (data) => {
      queryClient.setQueryData(queryKey, data);
    },
  });

  async function handleRate(
    rating: FeedbackRating | null,
    reason?: string,
    options?: FeedbackRateOptions,
  ) {
    await mutation.mutateAsync({
      rating,
      reason,
      needsSupport: options?.needsSupport ?? false,
    });
  }

  // A click before the saved row arrives would overwrite its reason and help request with an empty write.
  if (isPending) {
    return null;
  }

  return (
    <FeedbackThumbs
      key={`${targetType}:${targetId}`}
      rating={saved?.rating ?? null}
      reason={saved?.reason ?? null}
      onRate={handleRate}
      prompt="Did this run do what you wanted?"
      supportOption={{ label: "I need help with this" }}
      variant={variant}
      className={className}
    />
  );
}

export { RunFeedback };
