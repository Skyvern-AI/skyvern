import { useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import {
  OnboardingTelemetry,
  type Surface,
} from "@/util/onboarding/OnboardingTelemetry";

type Action = {
  label: string;
  onClick: () => void;
};

type Props = {
  surface: Surface;
  icon: React.ReactNode;
  title: string;
  description: string;
  primaryAction: Action;
  secondaryAction?: Action;
};

function OnboardingEmptyState({
  surface,
  icon,
  title,
  description,
  primaryAction,
  secondaryAction,
}: Readonly<Props>) {
  const viewedRef = useRef(false);

  useEffect(() => {
    if (viewedRef.current) return;
    viewedRef.current = true;
    OnboardingTelemetry.emptyStateViewed(surface);
  }, [surface]);

  return (
    <div
      data-testid={`onboarding-empty-state-${surface}`}
      className="flex w-full flex-col items-center justify-center gap-4 py-16 text-center"
    >
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
        {icon}
      </div>
      <div className="max-w-sm space-y-2">
        <h3 className="text-lg font-medium">{title}</h3>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
      <div className="flex items-center gap-3 pt-2">
        <Button
          size="sm"
          onClick={() => {
            OnboardingTelemetry.emptyStateCTAClicked(
              surface,
              primaryAction.label,
            );
            primaryAction.onClick();
          }}
        >
          {primaryAction.label}
        </Button>
        {secondaryAction && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              OnboardingTelemetry.emptyStateCTAClicked(
                surface,
                secondaryAction.label,
              );
              secondaryAction.onClick();
            }}
          >
            {secondaryAction.label}
          </Button>
        )}
      </div>
    </div>
  );
}

export { OnboardingEmptyState };
