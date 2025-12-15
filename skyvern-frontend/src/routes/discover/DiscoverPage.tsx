import { PromptBox } from "../tasks/create/PromptBox";
import { WorkflowTemplates } from "./WorkflowTemplates";
import {
  OnboardingModal,
  useOnboarding,
} from "@/components/Onboarding/OnboardingModal";
import { isSupabaseEnabled } from "@/api/supabase";

function DiscoverPage() {
  const { showOnboarding, completeOnboarding } = useOnboarding();

  return (
    <div className="space-y-10">
      {isSupabaseEnabled && (
        <OnboardingModal open={showOnboarding} onComplete={completeOnboarding} />
      )}
      <PromptBox />
      <WorkflowTemplates />
    </div>
  );
}

export { DiscoverPage };
