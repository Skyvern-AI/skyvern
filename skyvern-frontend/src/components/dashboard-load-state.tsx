import {
  ExclamationTriangleIcon,
  InfoCircledIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import * as React from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/util/utils";

type DashboardLoadStateProps = {
  isLoading: boolean;
  isError: boolean;
  isEmpty: boolean;
  error?: unknown;
  retry?: () => void;
  surface: string;
  emptyCopy: string;
  skeleton?: React.ReactNode;
  containerClassName?: string;
  testId?: string;
};

const InfoIcon = InfoCircledIcon;

function errorDescription(error: unknown): string {
  if (error && typeof error === "object" && "message" in error) {
    const tail = String((error as { message: unknown }).message ?? "").trim();
    if (tail.length > 0) {
      return tail;
    }
  }
  return "Try again.";
}

function DashboardLoadState({
  isLoading,
  isError,
  isEmpty,
  error,
  retry,
  surface,
  emptyCopy,
  skeleton,
  containerClassName,
  testId,
}: DashboardLoadStateProps): React.ReactElement | null {
  // priority: loading > error > empty
  if (isLoading) {
    return (
      <div
        data-testid={testId ?? "dashboard-load-state"}
        data-state="loading"
        className={cn("w-full", containerClassName)}
      >
        {skeleton ?? <Skeleton className="h-32 w-full" />}
      </div>
    );
  }

  if (isError) {
    return (
      <div
        data-testid={testId ?? "dashboard-load-state"}
        data-state="error"
        className={cn("w-full", containerClassName)}
      >
        <Alert variant="destructive">
          <ExclamationTriangleIcon className="h-4 w-4" />
          <AlertTitle>Couldn&apos;t load {surface}</AlertTitle>
          <AlertDescription className="flex flex-col gap-3">
            <span>{errorDescription(error)}</span>
            {retry ? (
              <span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={retry}
                  data-testid={`${testId ?? "dashboard-load-state"}-retry`}
                  className="gap-2"
                >
                  <ReloadIcon className="h-3 w-3" aria-hidden="true" />
                  Try again
                </Button>
              </span>
            ) : null}
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  if (isEmpty) {
    return (
      <div
        data-testid={testId ?? "dashboard-load-state"}
        data-state="empty"
        className={cn(
          "flex w-full flex-col items-center justify-center gap-2 text-center",
          containerClassName,
        )}
      >
        <InfoIcon
          className="h-6 w-6 text-muted-foreground"
          aria-hidden="true"
        />
        <p className="text-sm text-muted-foreground">{emptyCopy}</p>
      </div>
    );
  }

  return null;
}

export { DashboardLoadState };
export type { DashboardLoadStateProps };
