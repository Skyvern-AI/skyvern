import type { ReactNode } from "react";
import { OnboardingStepProgress } from "./OnboardingStepProgress";

type Props = {
  stepIndex: number;
  stepCount: number;
  chip?: "optional" | "final";
  children: ReactNode;
};

function OnboardingStepShell(props: Readonly<Props>) {
  const { stepIndex, stepCount, chip, children } = props;
  return (
    <div className="grid min-h-0 grid-rows-[auto_auto_minmax(0,1fr)_auto_auto] gap-4">
      <OnboardingStepProgress
        stepIndex={stepIndex}
        stepCount={stepCount}
        chip={chip}
      />
      {children}
    </div>
  );
}

export { OnboardingStepShell };
