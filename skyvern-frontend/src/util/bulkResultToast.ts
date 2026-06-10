import { toast } from "@/components/ui/use-toast";

function firstRejectionMessage(
  results: PromiseSettledResult<unknown>[],
): string | undefined {
  const rejection = results.find(
    (result): result is PromiseRejectedResult => result.status === "rejected",
  );
  if (!rejection) {
    return undefined;
  }
  const reason: unknown = rejection.reason;
  if (reason && typeof reason === "object") {
    // API errors carry the useful text in the FastAPI detail, not the generic
    // axios "Request failed with status code N" message.
    const detail = (reason as { response?: { data?: { detail?: unknown } } })
      .response?.data?.detail;
    if (typeof detail === "string") {
      return detail;
    }
    const message = (reason as { message?: unknown }).message;
    return typeof message === "string" ? message : undefined;
  }
  return typeof reason === "string" ? reason : undefined;
}

function bulkResultToast({
  succeeded,
  total,
  results,
  successTitle,
  failureTitle,
  partialTitle,
}: {
  succeeded: number;
  total: number;
  results?: PromiseSettledResult<unknown>[];
  successTitle: (count: number) => string;
  failureTitle: (count: number) => string;
  partialTitle: (succeeded: number, failed: number) => string;
}) {
  const failed = total - succeeded;
  const description = results ? firstRejectionMessage(results) : undefined;
  if (failed === 0) {
    toast({
      title: successTitle(succeeded),
      variant: "success",
      description,
    });
    return;
  }
  if (succeeded === 0) {
    toast({
      title: failureTitle(failed),
      variant: "destructive",
      description,
    });
    return;
  }
  toast({
    title: partialTitle(succeeded, failed),
    variant: "warning",
    description,
  });
}

export { bulkResultToast };
