import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  RocketIcon,
  LightningBoltIcon,
  CodeIcon,
  CheckCircledIcon,
} from "@radix-ui/react-icons";

const ONBOARDING_COMPLETED_KEY = "jadongai_onboarding_completed";

interface OnboardingModalProps {
  open: boolean;
  onComplete: () => void;
}

const steps = [
  {
    icon: <RocketIcon className="h-12 w-12" />,
    title: "JadongAI에 오신 것을 환영합니다!",
    description:
      "AI 기반 브라우저 자동화 플랫폼에 오신 것을 환영합니다. 몇 가지 핵심 기능을 안내해 드리겠습니다.",
  },
  {
    icon: <LightningBoltIcon className="h-12 w-12" />,
    title: "자연어로 작업 생성",
    description:
      "복잡한 코드 없이 자연어로 원하는 작업을 설명하세요. '네이버에서 최신 뉴스 검색하기'처럼 간단히 입력하면 됩니다.",
  },
  {
    icon: <CodeIcon className="h-12 w-12" />,
    title: "워크플로우 빌더",
    description:
      "드래그 앤 드롭으로 복잡한 자동화 워크플로우를 구성할 수 있습니다. 여러 작업을 연결하여 강력한 자동화를 만들어보세요.",
  },
  {
    icon: <CheckCircledIcon className="h-12 w-12" />,
    title: "준비 완료!",
    description:
      "이제 첫 번째 자동화 작업을 만들어볼 준비가 되었습니다. '탐색' 메뉴에서 시작하거나 직접 작업을 생성해보세요.",
  },
];

export function OnboardingModal({ open, onComplete }: OnboardingModalProps) {
  const [currentStep, setCurrentStep] = useState(0);

  const handleNext = () => {
    if (currentStep < steps.length - 1) {
      setCurrentStep(currentStep + 1);
    } else {
      handleComplete();
    }
  };

  const handlePrevious = () => {
    if (currentStep > 0) {
      setCurrentStep(currentStep - 1);
    }
  };

  const handleComplete = () => {
    localStorage.setItem(ONBOARDING_COMPLETED_KEY, "true");
    onComplete();
  };

  const handleSkip = () => {
    handleComplete();
  };

  const step = steps[currentStep];
  const isLastStep = currentStep === steps.length - 1;

  return (
    <Dialog open={open} onOpenChange={() => {}}>
      <DialogContent className="sm:max-w-lg" hideCloseButton>
        <DialogHeader className="text-center">
          <div className="mx-auto mb-4 flex h-20 w-20 items-center justify-center rounded-full bg-gradient-to-r from-blue-500/20 to-purple-500/20 text-blue-400">
            {step.icon}
          </div>
          <DialogTitle className="text-2xl">{step.title}</DialogTitle>
          <DialogDescription className="text-base">
            {step.description}
          </DialogDescription>
        </DialogHeader>

        {/* Progress dots */}
        <div className="flex justify-center gap-2 py-4">
          {steps.map((_, index) => (
            <div
              key={index}
              className={`h-2 w-2 rounded-full transition-colors ${
                index === currentStep ? "bg-primary" : "bg-slate-600"
              }`}
            />
          ))}
        </div>

        <DialogFooter className="flex-col gap-2 sm:flex-row sm:justify-between">
          <div>
            {currentStep === 0 && (
              <Button variant="ghost" onClick={handleSkip}>
                건너뛰기
              </Button>
            )}
            {currentStep > 0 && (
              <Button variant="ghost" onClick={handlePrevious}>
                이전
              </Button>
            )}
          </div>
          <Button onClick={handleNext}>
            {isLastStep ? "시작하기" : "다음"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function useOnboarding() {
  const [showOnboarding, setShowOnboarding] = useState(() => {
    const completed = localStorage.getItem(ONBOARDING_COMPLETED_KEY);
    return completed !== "true";
  });

  const completeOnboarding = () => {
    setShowOnboarding(false);
  };

  return { showOnboarding, completeOnboarding };
}
