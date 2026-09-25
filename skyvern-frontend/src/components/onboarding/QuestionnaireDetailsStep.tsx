import { ChevronDownIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useId, useRef, useState } from "react";
import { z } from "zod";
import { cn } from "@/util/utils";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { OnboardingStepShell } from "./OnboardingStepShell";
import type {
  QuestionnaireAnswersV1,
  QuestionnaireCompanyContextV1,
  QuestionnairePatchV1,
  QuestionnaireReferralSourceDetailV1,
  QuestionnaireReferralSourceV1,
  QuestionnaireRoleV1,
  QuestionnaireScaleIntentV1,
  ProjectOwnerFieldsV1,
} from "@/store/onboarding/types";

type Option<Value extends string> = {
  value: Value;
  label: string;
};

type Selections = {
  [Key in keyof QuestionnaireAnswersV1]: QuestionnaireAnswersV1[Key] | "";
};

type Props = {
  completionAction: "complete" | "update";
  expectedRevision: number;
  organizationId?: string | null;
  initialAnswers?: QuestionnaireAnswersV1 | null;
  externalError?: string | null;
  isPending: boolean;
  onAction: (patch: QuestionnairePatchV1) => Promise<void>;
  onBack: () => void;
};

type SelectFieldProps<Value extends string> = {
  id: string;
  label: string;
  placeholder: string;
  value: Value | "";
  choices: readonly Option<Value>[];
  disabled: boolean;
  onValueChange: (value: Value) => void;
  onClear?: () => void;
};

function optionsFromLabels<Value extends string>(
  labels: Record<Value, string>,
): readonly Option<Value>[] {
  return (Object.keys(labels) as Value[]).map((value) => ({
    value,
    label: labels[value],
  }));
}

const ROLE_LABELS: Record<QuestionnaireRoleV1, string> = {
  developer: "Developer",
  technical_operator: "Technical operator",
  business_operator: "Business operator",
  product_manager: "Product manager",
  founder_or_executive: "Founder or executive",
  other: "Other",
  prefer_not_to_say: "Prefer not to say",
};
const ROLE_OPTIONS = optionsFromLabels(ROLE_LABELS);

const COMPANY_CONTEXT_LABELS: Record<QuestionnaireCompanyContextV1, string> = {
  personal_or_individual: "Personal or individual",
  startup: "Startup",
  agency_or_services: "Agency or services",
  established_company: "Established company",
  education_or_research: "Education or research",
  other: "Other",
  prefer_not_to_say: "Prefer not to say",
};
const COMPANY_CONTEXT_OPTIONS = optionsFromLabels(COMPANY_CONTEXT_LABELS);

const SCALE_INTENT_LABELS: Record<QuestionnaireScaleIntentV1, string> = {
  exploring: "Exploring",
  single_workflow: "A single workflow",
  recurring_individual: "Recurring individual workflows",
  team_or_multi_workflow: "Team or multiple workflows",
  production_high_volume: "Production or high volume",
  unsure: "Unsure",
};
const SCALE_INTENT_OPTIONS = optionsFromLabels(SCALE_INTENT_LABELS);

const REFERRAL_SOURCE_LABELS: Record<QuestionnaireReferralSourceV1, string> = {
  search: "Search",
  social: "Social media",
  ai_assistant: "AI assistant",
  friend_or_colleague: "Friend or colleague",
  video: "Video",
  blog_or_article: "Blog or article",
  event_or_community: "Event or community",
  podcast: "Podcast",
  other: "Other",
  prefer_not_to_say: "Prefer not to say",
};
const REFERRAL_SOURCE_OPTIONS = optionsFromLabels(REFERRAL_SOURCE_LABELS);

const REFERRAL_SOURCE_DETAIL_OPTIONS: Partial<
  Record<
    QuestionnaireReferralSourceV1,
    readonly Option<QuestionnaireReferralSourceDetailV1>[]
  >
> = {
  ai_assistant: optionsFromLabels({
    chatgpt: "ChatGPT",
    claude: "Claude",
    perplexity: "Perplexity",
    gemini: "Gemini",
    other_ai_assistant: "Other AI assistant",
  }),
  social: optionsFromLabels({
    reddit: "Reddit",
    x: "X (Twitter)",
    linkedin: "LinkedIn",
    youtube: "YouTube",
    other_social: "Other social network",
  }),
};

function selectedAnswers(
  selections: Selections,
): QuestionnaireAnswersV1 | null {
  const {
    role,
    company_context,
    scale_intent,
    referral_source,
    referral_source_detail,
  } = selections;
  return role && company_context && scale_intent && referral_source
    ? {
        role,
        company_context,
        scale_intent,
        referral_source,
        ...(REFERRAL_SOURCE_DETAIL_OPTIONS[referral_source]
          ? { referral_source_detail: referral_source_detail || null }
          : {}),
      }
    : null;
}

function QuestionnaireSelect<Value extends string>({
  id,
  label,
  placeholder,
  value,
  choices,
  disabled,
  onValueChange,
  onClear,
}: Readonly<SelectFieldProps<Value>>) {
  function selectValue(nextValue: string) {
    const choice = choices.find((candidate) => candidate.value === nextValue);
    if (choice) {
      onValueChange(choice.value);
    }
  }

  return (
    <div className="grid gap-2">
      <div
        className={cn(
          "flex items-center justify-between gap-2",
          onClear && "min-h-11",
        )}
      >
        <Label htmlFor={id} className="text-sm leading-snug">
          {label}
        </Label>
        {onClear && value && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label={`Clear ${label}`}
            className="min-h-11 min-w-11 touch-manipulation motion-reduce:transition-none"
            disabled={disabled}
            onClick={onClear}
          >
            Clear
          </Button>
        )}
      </div>
      <Select value={value} onValueChange={selectValue} disabled={disabled}>
        <SelectTrigger
          id={id}
          className="min-h-11 w-full touch-manipulation bg-background text-foreground motion-reduce:transition-none"
        >
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent className="z-[2147480004]">
          {choices.map((choice) => (
            <SelectItem
              key={choice.value}
              value={choice.value}
              className="min-h-11 touch-manipulation motion-reduce:transition-none"
            >
              {choice.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function QuestionnaireDetailsStep({
  completionAction,
  expectedRevision,
  organizationId,
  initialAnswers,
  externalError,
  isPending,
  onAction,
  onBack,
}: Readonly<Props>) {
  const fieldPrefix = useId();
  const incompleteHintId = `${fieldPrefix}-incomplete-hint`;
  const [selections, setSelections] = useState<Selections>(() => ({
    role: initialAnswers?.role ?? "",
    company_context: initialAnswers?.company_context ?? "",
    scale_intent: initialAnswers?.scale_intent ?? "",
    referral_source: initialAnswers?.referral_source ?? "",
    referral_source_detail: initialAnswers?.referral_source_detail ?? "",
  }));
  const [pendingAction, setPendingAction] = useState<
    QuestionnairePatchV1["action"] | null
  >(null);
  const [error, setError] = useState<string | null>(null);
  const [ownerExpanded, setOwnerExpanded] = useState(false);
  const [ownerDraft, setOwnerDraft] = useState({
    name: "",
    professional_email: "",
    role: "",
  });
  const [ownerError, setOwnerError] = useState<string | null>(null);
  const displayedError = externalError ?? error;
  const pending = isPending || pendingAction !== null;
  const mutationIdentityRef = useRef<{
    payload: string;
    mutationId: string;
  } | null>(null);
  const answers = selectedAnswers(selections);
  const detailOptions = selections.referral_source
    ? REFERRAL_SOURCE_DETAIL_OPTIONS[selections.referral_source]
    : undefined;

  function setSelection<Key extends keyof QuestionnaireAnswersV1>(
    key: Key,
    value: QuestionnaireAnswersV1[Key],
  ) {
    setSelections((current) => ({ ...current, [key]: value }));
  }

  function mutationIdFor(payload: string) {
    const mutationId =
      mutationIdentityRef.current?.payload === payload
        ? mutationIdentityRef.current.mutationId
        : crypto.randomUUID();
    mutationIdentityRef.current = { payload, mutationId };
    return mutationId;
  }

  function discardOwner() {
    setOwnerDraft({ name: "", professional_email: "", role: "" });
    setOwnerError(null);
  }

  async function submit(
    action: QuestionnairePatchV1["action"],
    discard = false,
  ) {
    if (pending) return;
    const owner: ProjectOwnerFieldsV1 = {};
    if (action === "skip" || discard) {
      discardOwner();
    } else {
      for (const key of ["name", "professional_email", "role"] as const) {
        const value = ownerDraft[key].trim();
        if (value) owner[key] = value;
      }
      if (
        owner.professional_email &&
        !z.string().max(254).email().safeParse(owner.professional_email).success
      ) {
        setOwnerError("Enter a valid work email or discard the owner details.");
        setOwnerExpanded(true);
        return;
      }
      if (Object.keys(owner).length > 0 && !organizationId) {
        setOwnerError(
          "We couldn't confirm your organization. Discard the owner details to continue.",
        );
        setOwnerExpanded(true);
        return;
      }
    }

    try {
      const payload =
        action === "skip"
          ? { version: 1 as const, expected_revision: expectedRevision, action }
          : answers && {
              version: 1 as const,
              expected_revision: expectedRevision,
              action,
              ...answers,
              ...(Object.keys(owner).length > 0 && organizationId
                ? {
                    project_owner: {
                      action: "set" as const,
                      expected_organization_id: organizationId,
                      ...owner,
                    },
                  }
                : {}),
            };
      if (!payload) return;
      const patch = {
        ...payload,
        mutation_id: mutationIdFor(JSON.stringify(payload)),
      } as QuestionnairePatchV1;

      setPendingAction(action);
      setError(null);
      await onAction(patch);
      mutationIdentityRef.current = null;
    } catch (cause) {
      if (cause instanceof Error && cause.message === "project_owner_invalid") {
        setOwnerError("Check the owner details or discard them to continue.");
        setOwnerExpanded(true);
      } else {
        setError("We couldn't save your details. Try again.");
      }
    } finally {
      setPendingAction(null);
    }
  }

  return (
    <OnboardingStepShell stepIndex={2} stepCount={2} chip="final">
      <DialogHeader className="pr-12">
        <DialogTitle className="text-xl font-semibold">
          Tell us about your setup
        </DialogTitle>
        <DialogDescription>
          Optional. Your answers help us recommend templates and guidance. They
          never limit what you can build.
        </DialogDescription>
      </DialogHeader>
      <div className="grid min-h-0 gap-4 overflow-y-auto overscroll-contain pb-6 pr-1 pt-1">
        <QuestionnaireSelect
          id={`${fieldPrefix}-role`}
          label="What best describes your role?"
          placeholder="Choose a role"
          value={selections.role}
          choices={ROLE_OPTIONS}
          disabled={pending}
          onValueChange={(value) => setSelection("role", value)}
        />
        <QuestionnaireSelect
          id={`${fieldPrefix}-company-context`}
          label="What kind of organization are you part of?"
          placeholder="Choose an organization type"
          value={selections.company_context}
          choices={COMPANY_CONTEXT_OPTIONS}
          disabled={pending}
          onValueChange={(value) => setSelection("company_context", value)}
        />
        <QuestionnaireSelect
          id={`${fieldPrefix}-scale-intent`}
          label="How do you plan to use Skyvern?"
          placeholder="Choose a usage pattern"
          value={selections.scale_intent}
          choices={SCALE_INTENT_OPTIONS}
          disabled={pending}
          onValueChange={(value) => setSelection("scale_intent", value)}
        />
        <QuestionnaireSelect
          id={`${fieldPrefix}-referral-source`}
          label="How did you hear about Skyvern?"
          placeholder="Choose a source"
          value={selections.referral_source}
          choices={REFERRAL_SOURCE_OPTIONS}
          disabled={pending}
          onValueChange={(value) =>
            setSelections((current) => ({
              ...current,
              referral_source: value,
              referral_source_detail: REFERRAL_SOURCE_DETAIL_OPTIONS[
                value
              ]?.some(
                (option) => option.value === current.referral_source_detail,
              )
                ? current.referral_source_detail
                : "",
            }))
          }
        />
        {detailOptions && (
          <QuestionnaireSelect
            id={`${fieldPrefix}-referral-source-detail`}
            label="Which one?"
            placeholder="Select one (optional)"
            value={selections.referral_source_detail ?? ""}
            choices={detailOptions}
            disabled={pending}
            onValueChange={(value) =>
              setSelection("referral_source_detail", value)
            }
            onClear={() =>
              setSelections((current) => ({
                ...current,
                referral_source_detail: "",
              }))
            }
          />
        )}
        {organizationId ? (
          <div className="ph-no-capture grid gap-3 border-t border-border pt-3">
            <Button
              type="button"
              variant="ghost"
              className="h-auto min-h-11 justify-start whitespace-normal px-0 text-left"
              aria-expanded={ownerExpanded}
              aria-controls={`${fieldPrefix}-owner`}
              disabled={pending}
              onClick={() => setOwnerExpanded(!ownerExpanded)}
            >
              <ChevronDownIcon
                aria-hidden
                className={cn(
                  "mr-2 size-4 shrink-0",
                  ownerExpanded && "rotate-180",
                )}
              />
              Who else owns this automation project? (Optional)
            </Button>
            {ownerExpanded ? (
              <div id={`${fieldPrefix}-owner`} className="grid gap-3">
                <p className="text-sm text-muted-foreground">
                  Share a project owner or technical sponsor to help us
                  understand your project. This does not send an invitation or
                  message.
                </p>
                {(
                  [
                    ["name", "Name", 120],
                    ["professional_email", "Work email", 254],
                    ["role", "Role", 120],
                  ] as const
                ).map(([key, label, maxLength]) => (
                  <div key={key} className="grid gap-2">
                    <Label htmlFor={`${fieldPrefix}-owner-${key}`}>
                      {label}
                    </Label>
                    <Input
                      id={`${fieldPrefix}-owner-${key}`}
                      type={key === "professional_email" ? "email" : "text"}
                      autoComplete="off"
                      maxLength={maxLength}
                      value={ownerDraft[key]}
                      disabled={pending}
                      className="min-h-11"
                      aria-invalid={
                        (key === "professional_email" && !!ownerError) ||
                        undefined
                      }
                      aria-describedby={
                        ownerError ? `${fieldPrefix}-owner-error` : undefined
                      }
                      onChange={(event) => {
                        const value = event.target.value;
                        setOwnerDraft((current) => ({
                          ...current,
                          [key]: value,
                        }));
                        setOwnerError(null);
                      }}
                    />
                  </div>
                ))}
                {ownerError ? (
                  <div className="grid gap-2">
                    <p
                      id={`${fieldPrefix}-owner-error`}
                      role="alert"
                      className="text-sm text-destructive"
                    >
                      {ownerError}
                    </p>
                    <Button
                      type="button"
                      variant="outline"
                      disabled={pending}
                      onClick={() => void submit(completionAction, true)}
                    >
                      Discard owner details and continue
                    </Button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
        {displayedError ? (
          <Alert variant="destructive">
            <AlertDescription>{displayedError}</AlertDescription>
          </Alert>
        ) : null}
        {!answers ? (
          <p id={incompleteHintId} className="text-sm text-muted-foreground">
            Answer the four setup questions to complete and continue.
          </p>
        ) : null}
      </div>
      <DialogFooter className="sticky bottom-0 z-10 -mx-6 -mb-6 flex-col gap-2 border-t border-border bg-background px-6 pb-[max(1rem,env(safe-area-inset-bottom))] pt-4 sm:flex-row sm:justify-between sm:gap-0 sm:space-x-0">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="min-h-11 w-full min-w-11 touch-manipulation motion-reduce:transition-none sm:w-auto"
          disabled={pending}
          onClick={onBack}
        >
          Back
        </Button>
        {completionAction === "complete" ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="min-h-11 w-full min-w-11 touch-manipulation motion-reduce:transition-none sm:w-auto"
            disabled={pending}
            onClick={() => void submit("skip")}
          >
            {pendingAction === "skip" ? (
              <ReloadIcon
                aria-hidden
                className="mr-2 size-4 animate-spin motion-reduce:animate-none"
              />
            ) : null}
            Skip
          </Button>
        ) : null}
        <Button
          type="button"
          size="sm"
          className={cn(
            "min-h-11 w-full min-w-11 touch-manipulation motion-reduce:transition-none sm:w-auto",
            !answers && "cursor-not-allowed opacity-50",
          )}
          aria-describedby={!answers ? incompleteHintId : undefined}
          aria-disabled={!answers || undefined}
          disabled={pending}
          onClick={() => void submit(completionAction)}
        >
          {pendingAction === completionAction ? (
            <ReloadIcon
              aria-hidden
              className="mr-2 size-4 animate-spin motion-reduce:animate-none"
            />
          ) : null}
          Complete and continue
        </Button>
      </DialogFooter>
    </OnboardingStepShell>
  );
}

export { QuestionnaireDetailsStep };
