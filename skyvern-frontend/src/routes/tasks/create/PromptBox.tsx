import { getClient } from "@/api/AxiosClient";
import { isPaymentRequiredError } from "@/api/paymentRequired";
import { Createv2TaskRequest } from "@/api/types";
import { stringify as convertToYAML } from "yaml";
import { WorkflowCreateYAMLRequest } from "@/routes/workflows/types/workflowYamlTypes";
import img from "@/assets/promptBoxBg.png";
import { CartIcon } from "@/components/icons/CartIcon";
import { GraphIcon } from "@/components/icons/GraphIcon";
import { InboxIcon } from "@/components/icons/InboxIcon";
import { ToastAction } from "@/components/ui/toast";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { useBrowserSessionPrewarm } from "./useBrowserSessionPrewarm";
import {
  ArrowUpIcon,
  CheckIcon,
  ChevronDownIcon,
  Cross2Icon,
  EnvelopeClosedIcon,
  FileTextIcon,
  GlobeIcon,
  GearIcon,
  PlusIcon,
  ReloadIcon,
  TextAlignLeftIcon,
  UploadIcon,
  VideoIcon,
} from "@radix-ui/react-icons";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError, type AxiosResponse } from "axios";
import {
  forwardRef,
  type ForwardedRef,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  CapabilityExamples,
  JOB_APPLICATION_PROMPT,
  SAMPLE_RESUME_PATH,
  SAMPLE_RESUME_PUBLIC_URL,
} from "./CapabilityExamples";
import { ExampleCasePill } from "./ExampleCasePill";
import { CyclingPlaceholderTextarea } from "./CyclingPlaceholderTextarea";
import {
  AdvancedSettingsPopover,
  ChangedSettingsChips,
} from "./PromptBoxAdvancedSettings";
import {
  DEFAULT_TASK_RUN_SETTINGS,
  type SettingsTab,
  type TaskRunSettings,
} from "./taskRunSettings";
import type { CopilotAttachedFile } from "@/routes/workflows/copilot/workflowCopilotTypes";
import { HomeTelemetry, type AgentCreationAttempt } from "@/util/homeTelemetry";
import { useAutoplayStore } from "@/store/useAutoplayStore";
import { SpeechInputButton } from "@/components/SpeechInputButton";
import { getErrorDetail } from "@/util/getErrorDetail";
import { cn } from "@/util/utils";
import { useSpeechToTextField } from "@/hooks/useSpeechToTextField";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { rememberDiscoverCopilotPrompt } from "@/routes/workflows/discoverCopilotHandoff";
import { workflowEditorPath } from "@/routes/workflows/studioNavigation";

// Most-clicked first; the grid order is the ranking users see.
const exampleCases = [
  {
    key: "job_application",
    hint: "jobs.lever.co",
    label: "Apply for a job",
    prompt: JOB_APPLICATION_PROMPT,
    attachment: SAMPLE_RESUME_PATH,
    icon: <InboxIcon className="size-6" />,
  },
  {
    key: "finditparts",
    hint: "finditparts.com",
    label: "Add a product to cart",
    prompt:
      'Go to https://www.finditparts.com first. Search for the product "W01-377-8537", add it to cart and then navigate to the cart page. Your goal is COMPLETE when you\'re on the cart page and the specified product is in the cart. Extract all product quantity information from the cart page. Do not attempt to checkout.',
    icon: <CartIcon className="size-6" />,
  },
  {
    key: "contact_us_forms",
    hint: "canadahvac.com",
    label: "Fill a contact us form",
    prompt: `Go to https://canadahvac.com/contact-hvac-canada. Fill out the contact us form and submit it. Your goal is complete when the page says your message has been sent. Here's the user information: {"name":"John Doe","email":"john.doe@gmail.com","phone":"123-456-7890","message":"Hello, I have a question about your services."}`,
    icon: <EnvelopeClosedIcon className="size-6" />,
  },
  {
    key: "geico",
    hint: "geico.com",
    label: "Get an insurance quote",
    prompt: `Go to https://www.geico.com first. Navigate through the website until you generate an auto insurance quote. Do not generate a home insurance quote. If you're on a page showing an auto insurance quote (with premium amounts), your goal is COMPLETE. Extract all quote information in JSON format including the premium amount, the timeframe for the quote. Here's the user information: {"licensed_at_age":19,"education_level":"HIGH_SCHOOL","phone_number":"8042221111","full_name":"Chris P. Bacon","past_claim":[],"has_claims":false,"spouse_occupation":"Florist","auto_current_carrier":"None","home_commercial_uses":null,"spouse_full_name":"Amy Stake","auto_commercial_uses":null,"requires_sr22":false,"previous_address_move_date":null,"line_of_work":null,"spouse_age":"1987-12-12","auto_insurance_deadline":null,"email":"chris.p.bacon@abc.com","net_worth_numeric":1000000,"spouse_gender":"F","marital_status":"married","spouse_licensed_at_age":20,"license_number":"AAAAAAA090AA","spouse_license_number":"AAAAAAA080AA","how_much_can_you_lose":25000,"vehicles":[{"annual_mileage":10000,"commute_mileage":4000,"existing_coverages":null,"ideal_coverages":{"bodily_injury_per_incident_limit":50000,"bodily_injury_per_person_limit":25000,"collision_deductible":1000,"comprehensive_deductible":1000,"personal_injury_protection":null,"property_damage_per_incident_limit":null,"property_damage_per_person_limit":25000,"rental_reimbursement_per_incident_limit":null,"rental_reimbursement_per_person_limit":null,"roadside_assistance_limit":null,"underinsured_motorist_bodily_injury_per_incident_limit":50000,"underinsured_motorist_bodily_injury_per_person_limit":25000,"underinsured_motorist_property_limit":null},"ownership":"Owned","parked":"Garage","purpose":"commute","vehicle":{"style":"AWD 3.0 quattro TDI 4dr Sedan","model":"A8 L","price_estimate":29084,"year":2015,"make":"Audi"},"vehicle_id":null,"vin":null}],"additional_drivers":[],"home":[{"home_ownership":"owned"}],"spouse_line_of_work":"Agriculture, Forestry and Fishing","occupation":"Customer Service Representative","id":null,"gender":"M","credit_check_authorized":false,"age":"1987-11-11","license_state":"Washington","cash_on_hand":"$10000–14999","address":{"city":"HOUSTON","country":"US","state":"TX","street":"9625 GARFIELD AVE.","zip":"77082"},"spouse_education_level":"MASTERS","spouse_email":"amy.stake@abc.com","spouse_added_to_auto_policy":true}`,
    icon: <FileTextIcon className="size-6" />,
  },
  {
    key: "extractIntegrationsFromGong",
    hint: "gong.io",
    label: "Extract integrations from Gong",
    prompt:
      "Go to https://www.gong.io first. Navigate to the 'Integrations' page on the Gong website. Extract the names and descriptions of all integrations listed on the Gong integrations page. Ensure not to click on any external links or advertisements.",
    icon: <GearIcon className="size-6" />,
  },
  {
    key: "AAPLStockPrice",
    hint: "google.com/finance",
    label: "Search for AAPL on Google Finance",
    prompt:
      'Go to google finance and find the "AAPL" stock price. COMPLETE when the search results for "AAPL" are displayed and the stock price is extracted.',
    icon: <GraphIcon className="size-6" />,
  },
] as const;

type ExamplePromptKey = (typeof exampleCases)[number]["key"];
type ExampleAttribution = { id: string; edited: boolean };

const UPLOAD_RETENTION_DAYS = 30;
// Mirrors MAX_ATTACHED_FILES_PER_MESSAGE on the copilot chat request.
const MAX_HOME_ATTACHMENTS = 20;
// A failed /customer load leaves the flag unknown for the whole session, so stop waiting and show the flag-off controls.
const HANDOFF_FLAG_WAIT_MS = 3000;

const HOW_IT_WORKS = [
  {
    title: "Describe",
    body: "Write the task in plain language, with the site and the outcome you want.",
    icon: <TextAlignLeftIcon className="size-[15px]" />,
  },
  {
    title: "Skyvern drives a browser",
    body: "It opens a real browser, navigates, types, clicks, and handles logins and 2FA.",
    icon: <GlobeIcon className="size-[15px]" />,
  },
  {
    title: "You get the result or data",
    body: "A confirmation, a downloaded file, or structured JSON you can send anywhere.",
    icon: <CheckIcon className="size-[15px]" />,
  },
];

type PromptBoxProps = {
  enableCopilotHandoff?: boolean;
  /** Hides the toolbar controls until `enableCopilotHandoff` is known (at most HANDOFF_FLAG_WAIT_MS), so they don't swap on load. */
  handoffFlagLoading?: boolean;
  /** Home-screen variant: no prompt improver and no advanced settings. */
  minimal?: boolean;
  /** Fires once an agent has been created from this prompt box. */
  onAgentCreated?: () => void;
  /** Rendered under the prompt input in the full (non-minimal) layout. */
  secondaryAction?: ReactNode;
};

type PromptBoxHandle = {
  /** Prefills `key`, or `fallback` when `key` is not a known example (e.g. from a URL). */
  focusAndPrefillExample: (
    key: string | null,
    fallback: ExamplePromptKey,
  ) => void;
  /** Prefills the user's own words; never overwrites a prompt already typed. */
  focusAndPrefillPrompt: (text: string) => void;
};

function blankToNull(value: string | null): string | null {
  return value?.trim() || null;
}

function buildBlankWorkflowRequest(
  title: string,
  runWith: "agent" | "code" = "agent",
): WorkflowCreateYAMLRequest {
  return {
    title,
    description: "",
    ai_fallback: true,
    code_version: 2,
    run_with: runWith,
    workflow_definition: {
      version: 2,
      blocks: [],
      parameters: [],
    },
  };
}

function hasWorkflowShape(data: unknown): data is WorkflowApiResponse {
  if (typeof data !== "object" || data === null) {
    return false;
  }
  const candidate = data as Partial<WorkflowApiResponse>;
  const definition = candidate.workflow_definition;
  return (
    typeof candidate.workflow_permanent_id === "string" &&
    typeof definition === "object" &&
    definition !== null &&
    Array.isArray(definition.blocks)
  );
}

// Axios yields a raw string for any 2xx whose body fails JSON.parse, and
// `"".workflow_definition` is undefined — so an empty body or an edge/auth
// interstitial reaches onSuccess looking like a workflow. Describe the
// envelope only; the body may be an interstitial or carry customer data.
function describeResponseEnvelope(response: AxiosResponse<unknown>): string {
  const body: unknown = response.data;
  const parsedAsJson = typeof body === "object" && body !== null;
  const bodyLength =
    typeof body === "string"
      ? body.length
      : JSON.stringify(body ?? null).length;
  const contentType = String(response.headers?.["content-type"] ?? "unknown");
  return `status=${response.status} content_type=${contentType} body_length=${bodyLength} parsed_as_json=${parsedAsJson}`;
}

function showCreateErrorToast(title: string, error: unknown) {
  if (isPaymentRequiredError(error)) {
    toast({
      variant: "destructive",
      title: "Not enough credits",
      description: getErrorDetail(error),
      action: (
        <ToastAction altText="Go to Billing" asChild>
          <Link to="/billing">Go to Billing</Link>
        </ToastAction>
      ),
    });
    return;
  }
  toast({ variant: "destructive", title, description: getErrorDetail(error) });
}

function PromptBoxImpl(
  {
    enableCopilotHandoff = false,
    handoffFlagLoading = false,
    minimal = false,
    onAgentCreated,
    secondaryAction,
  }: PromptBoxProps,
  ref: ForwardedRef<PromptBoxHandle>,
) {
  const navigate = useNavigate();
  const studioEnabled = useWorkflowStudioEnabled();
  const [prompt, setPrompt] = useState<string>("");
  const [exampleAttribution, setExampleAttribution] = useState<
    ExampleAttribution | undefined
  >();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [taskRunSettings, setTaskRunSettings] = useState<TaskRunSettings>(
    DEFAULT_TASK_RUN_SETTINGS,
  );
  const prewarmBrowserSession = useBrowserSessionPrewarm(
    enableCopilotHandoff ? null : taskRunSettings.proxyLocation,
  );
  useEffect(() => {
    prewarmBrowserSession(prompt);
  }, [prewarmBrowserSession, prompt]);
  const [showAdvancedSettings, setShowAdvancedSettings] = useState(false);
  const [advancedSettingsTab, setAdvancedSettingsTab] =
    useState<SettingsTab>("run");
  const [showHowItWorks, setShowHowItWorks] = useState(false);
  const [promptTouched, setPromptTouched] = useState(false);
  const [attachedFiles, setAttachedFiles] = useState<CopilotAttachedFile[]>([]);
  const [handoffFlagWaitExpired, setHandoffFlagWaitExpired] = useState(false);
  useEffect(() => {
    if (!handoffFlagLoading) return;
    const timer = window.setTimeout(
      () => setHandoffFlagWaitExpired(true),
      HANDOFF_FLAG_WAIT_MS,
    );
    return () => window.clearTimeout(timer);
  }, [handoffFlagLoading]);
  const hideFlagControls = handoffFlagLoading && !handoffFlagWaitExpired;
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { setAutoplay } = useAutoplayStore();
  // react-query isPending only flips on the next render, so a same-frame
  // double-click can slip past it; the ref is the synchronous guard.
  const submitInFlightRef = useRef(false);

  const updatePrompt = useCallback((value: string) => {
    setPrompt(value);
    setExampleAttribution((current) => {
      if (!value.trim()) return undefined;
      if (!current || current.edited) return current;
      return { ...current, edited: true };
    });
  }, []);

  useImperativeHandle(ref, () => ({
    focusAndPrefillExample: (key, fallback) => {
      const selectedExample =
        exampleCases.find((example) => example.key === key) ??
        exampleCases.find((example) => example.key === fallback) ??
        exampleCases[0];
      if (!prompt.trim()) {
        cancelSpeech();
        setPrompt(
          withExampleAttachment(
            selectedExample.prompt,
            "attachment" in selectedExample
              ? selectedExample.attachment
              : undefined,
          ),
        );
        setExampleAttribution({ id: selectedExample.key, edited: false });
      }
      textareaRef.current?.scrollIntoView?.({ block: "center" });
      textareaRef.current?.focus({ preventScroll: true });
    },
    focusAndPrefillPrompt: (text) => {
      if (!prompt.trim()) {
        setPrompt(text);
        setExampleAttribution(undefined);
      }
      textareaRef.current?.scrollIntoView?.({ block: "center" });
      textareaRef.current?.focus({ preventScroll: true });
    },
  }));

  const uploadDocumentMutation = useMutation({
    mutationFn: async (source: File | string) => {
      let file = source;
      if (typeof file === "string") {
        const response = await fetch(file);
        if (!response.ok) {
          throw new Error(`Failed to load ${file} (${response.status})`);
        }
        file = new File([await response.blob()], file.split("/").pop()!, {
          type: "application/pdf",
        });
      }
      const client = await getClient(credentialGetter);
      const formData = new FormData();
      formData.append("file", file);
      // Bounded so an abandoned upload does not linger until the org retention policy.
      formData.append("retention_days", String(UPLOAD_RETENTION_DAYS));
      const result = await client.post<FormData, { data: { file_id: string } }>(
        "/upload_file",
        formData,
        { headers: { "Content-Type": "multipart/form-data" } },
      );
      return {
        file_id: result.data.file_id,
        filename: file.name,
        size_bytes: file.size,
        available: true,
      } satisfies CopilotAttachedFile;
    },
    onSuccess: (attached) => {
      HomeTelemetry.uploadDocumentFinished(true);
      setAttachedFiles((current) =>
        current.length >= MAX_HOME_ATTACHMENTS
          ? current
          : [...current, attached],
      );
      textareaRef.current?.focus();
    },
    onError: (error: unknown) => {
      HomeTelemetry.uploadDocumentFinished(false);
      showCreateErrorToast("Failed to upload file", error);
    },
  });

  // Returns the prompt to load. Attachments only reach the copilot handoff path,
  // so the plain task path gets the file's public URL in the prompt instead.
  const withExampleAttachment = (prompt: string, path?: string) => {
    const filename = path?.split("/").pop();
    if (!path || !filename) return prompt;
    if (!enableCopilotHandoff) {
      return `${prompt} The attached resume is at ${SAMPLE_RESUME_PUBLIC_URL}; download it from there.`;
    }
    if (attachedFiles.length >= MAX_HOME_ATTACHMENTS) {
      toast({
        variant: "destructive",
        title: "Too many attachments",
        description: "Remove an attachment to add the example's resume.",
      });
      return prompt;
    }
    // Starting the upload here (not after a fetch) flips isPending before the next render,
    // which disables submit and the example buttons until the resume is attached.
    if (!attachedFiles.some((file) => file.filename === filename)) {
      uploadDocumentMutation.mutate(path);
    }
    return prompt;
  };

  const recordTaskMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(buildBlankWorkflowRequest("Recorded Agent"));
      const result = await client.post<string, AxiosResponse<unknown>>(
        "/workflows",
        yaml,
        { headers: { "Content-Type": "text/plain" } },
      );
      if (!hasWorkflowShape(result.data)) {
        throw new Error(
          `workflow create returned an unexpected response shape (${describeResponseEnvelope(result)})`,
        );
      }
      return result.data;
    },
    onSuccess: (workflow) => {
      onAgentCreated?.();
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      navigate(
        workflowEditorPath(
          workflow.workflow_permanent_id,
          studioEnabled,
          "?record=1",
        ),
      );
    },
    onError: (error: AxiosError) => {
      showCreateErrorToast("Could not start recording", error);
    },
  });

  const generateWorkflowMutation = useMutation({
    mutationFn: async ({
      prompt,
    }: {
      prompt: string;
      attempt: AgentCreationAttempt;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const {
        proxyLocation,
        publishWorkflow,
        generateScript,
        maxStepsOverride,
      } = taskRunSettings;
      // The popover shows whitespace-only values as unchanged, so send them as unset.
      const webhookCallbackUrl = blankToNull(
        taskRunSettings.webhookCallbackUrl,
      );
      const maxScreenshotScrolls = blankToNull(
        taskRunSettings.maxScreenshotScrolls,
      );
      const dataSchema = blankToNull(taskRunSettings.dataSchema);
      const extraHttpHeaders = blankToNull(taskRunSettings.extraHttpHeaders);
      const totpIdentifier = taskRunSettings.totpIdentifier.trim();
      const request: Record<string, unknown> = {
        user_prompt: prompt,
        webhook_callback_url: webhookCallbackUrl,
        proxy_location: proxyLocation,
        totp_identifier: totpIdentifier,
        max_screenshot_scrolls: maxScreenshotScrolls,
        publish_workflow: publishWorkflow,
        generate_script: generateScript,
        run_with: "agent",
        ai_fallback: true,
        extracted_information_schema: dataSchema
          ? (() => {
              try {
                return JSON.parse(dataSchema);
              } catch (e) {
                return dataSchema;
              }
            })()
          : null,
        extra_http_headers: extraHttpHeaders
          ? (() => {
              try {
                return JSON.parse(extraHttpHeaders);
              } catch (e) {
                return extraHttpHeaders;
              }
            })()
          : null,
      };

      request.url = "https://google.com"; // a stand-in value; real url is generated via prompt

      const trimmedMaxStepsOverride = maxStepsOverride?.trim();
      const result = await client.post<
        Createv2TaskRequest,
        AxiosResponse<unknown>
      >(
        "/workflows/create-from-prompt",
        {
          task_version: "v1",
          request,
        },
        trimmedMaxStepsOverride
          ? { headers: { "x-max-steps-override": trimmedMaxStepsOverride } }
          : undefined,
      );

      if (!hasWorkflowShape(result.data)) {
        throw new Error(
          `create-from-prompt returned an unexpected response shape (${describeResponseEnvelope(result)})`,
        );
      }

      return result.data;
    },
    onSuccess: (workflow, { attempt }) => {
      HomeTelemetry.agentCreationSucceeded(
        attempt,
        workflow.workflow_permanent_id,
      );
      onAgentCreated?.();
      toast({
        variant: "success",
        title: "Agent Created",
        description: `Agent created successfully.`,
      });

      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });

      // The agent already exists server-side, so nothing between here and
      // navigate() may strand the user on /discover.
      const firstBlock = workflow.workflow_definition.blocks[0];

      if (firstBlock) {
        setAutoplay(workflow.workflow_permanent_id, firstBlock.label);
      }

      navigate(
        workflowEditorPath(workflow.workflow_permanent_id, studioEnabled),
      );
    },
    onError: (error: Error, { attempt }) => {
      HomeTelemetry.agentCreationFailed(attempt, error);
      showCreateErrorToast("Error creating agent from prompt", error);
    },
    onSettled: () => {
      submitInFlightRef.current = false;
    },
  });

  const handoffWorkflowMutation = useMutation({
    mutationFn: async ({
      prompt,
      runWith,
    }: {
      prompt: string;
      runWith: "agent" | "code";
      attempt: AgentCreationAttempt;
    }) => {
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(
        // A default title lets Copilot name the agent from this prompt on its first turn.
        buildBlankWorkflowRequest("New Agent", runWith),
      );
      const result = await client.post<string, AxiosResponse<unknown>>(
        "/workflows",
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        },
      );
      if (!hasWorkflowShape(result.data)) {
        throw new Error(
          `workflow create returned an unexpected response shape (${describeResponseEnvelope(result)})`,
        );
      }
      return { data: result.data, prompt };
    },
    onSuccess: ({ data: workflow, prompt }, { attempt }) => {
      HomeTelemetry.agentCreationSucceeded(
        attempt,
        workflow.workflow_permanent_id,
      );
      onAgentCreated?.();
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      // Only the studio handoff writes the recovery key. The legacy /build path
      // can mount Workspace, but it never consumes this stored discover seed.
      if (studioEnabled) {
        rememberDiscoverCopilotPrompt(workflow.workflow_permanent_id, prompt);
      }
      // `?via=discover` is what makes WorkflowEditor fire
      // `copilot.discover.started` with entry_point=discover on mount.
      navigate(
        workflowEditorPath(
          workflow.workflow_permanent_id,
          studioEnabled,
          "?via=discover",
        ),
        {
          state: {
            copilotMessage: prompt,
            copilotAttachedFiles:
              attachedFiles.length > 0 ? attachedFiles : undefined,
          },
        },
      );
    },
    onError: (error: AxiosError, { attempt }) => {
      HomeTelemetry.agentCreationFailed(attempt, error);
      showCreateErrorToast("Error creating agent", error);
    },
    onSettled: () => {
      submitInFlightRef.current = false;
    },
  });

  const isSubmitting =
    generateWorkflowMutation.isPending ||
    handoffWorkflowMutation.isPending ||
    uploadDocumentMutation.isPending ||
    recordTaskMutation.isPending;

  const {
    isSupported: isSpeechSupported,
    isListening: isSpeechListening,
    isHearingSpeech: isSpeechHearing,
    toggle: toggleSpeech,
    cancel: cancelSpeech,
  } = useSpeechToTextField({
    value: prompt,
    onChange: updatePrompt,
    enabled: !isSubmitting,
  });

  const submitPrompt = ({
    prompt,
    attribution,
  }: {
    prompt: string;
    attribution?: ExampleAttribution;
  }) => {
    if (submitInFlightRef.current || isSubmitting) {
      return;
    }
    submitInFlightRef.current = true;
    const source = attribution ? "example" : "typed";
    const example = attribution?.id;
    const exampleEdited = attribution?.edited;
    const attempt = HomeTelemetry.agentCreationSubmitted({
      source,
      example,
      exampleEdited,
      handoff: enableCopilotHandoff,
      variant: minimal ? "revamp" : "legacy",
    });
    HomeTelemetry.promptSubmitted({
      attemptId: attempt.attemptId,
      source,
      example,
      exampleEdited,
      promptLength: prompt.length,
      handoff: enableCopilotHandoff,
    });
    if (enableCopilotHandoff) {
      handoffWorkflowMutation.mutate({
        prompt,
        runWith: "agent",
        attempt,
      });
      return;
    }
    generateWorkflowMutation.mutate({ prompt, attempt });
  };

  const handlePromptKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // With no precise pointer (a phone) assume an on-screen keyboard, which has no Shift+Enter, so Return stays a
    // newline and the send button submits. A tablet with a keyboard but no trackpad falls in this bucket too.
    if (
      e.key !== "Enter" ||
      e.shiftKey ||
      e.nativeEvent.isComposing ||
      !window.matchMedia?.("(any-pointer: fine)").matches
    ) {
      return;
    }
    e.preventDefault();
    if (prompt.trim()) {
      submitPrompt({ prompt, attribution: exampleAttribution });
    }
  };

  const attachmentChips =
    attachedFiles.length > 0 ? (
      <div className="flex flex-wrap gap-1.5 px-1 pt-2">
        {attachedFiles.map((file) => (
          <span
            key={file.file_id}
            className="inline-flex items-center gap-1.5 rounded-md border border-input bg-slate-elevation2 px-2 py-1 text-xs text-foreground"
          >
            <FileTextIcon aria-hidden="true" className="size-3.5" />
            <span className="max-w-[16rem] truncate">{file.filename}</span>
            <button
              type="button"
              aria-label={`Remove ${file.filename}`}
              className="text-muted-foreground hover:text-foreground"
              onClick={() =>
                setAttachedFiles((current) =>
                  current.filter((f) => f.file_id !== file.file_id),
                )
              }
            >
              <Cross2Icon aria-hidden="true" className="size-3" />
            </button>
          </span>
        ))}
      </div>
    ) : null;

  const fileInput = (
    <input
      ref={fileInputRef}
      type="file"
      className="hidden"
      aria-label="Upload document"
      onChange={(event) => {
        const file = event.target.files?.[0];
        if (file) {
          uploadDocumentMutation.mutate(file);
        }
        event.target.value = "";
      }}
    />
  );

  const renderAddMenu = (sizeClassName: string) => (
    <DropdownMenu
      onOpenChange={(open) => {
        if (open) HomeTelemetry.addMenuOpened();
      }}
    >
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                aria-label="Add files and more"
                disabled={isSubmitting}
                className={cn(
                  "flex shrink-0 items-center justify-center rounded-full text-muted-foreground transition hover:bg-accent hover:text-accent-foreground disabled:opacity-50",
                  sizeClassName,
                )}
              >
                {uploadDocumentMutation.isPending ||
                recordTaskMutation.isPending ? (
                  <ReloadIcon className="size-4 animate-spin" />
                ) : (
                  <PlusIcon aria-hidden="true" className="size-[18px]" />
                )}
              </button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>Add files and more</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DropdownMenuContent align="start">
        {enableCopilotHandoff ? (
          <DropdownMenuItem
            disabled={attachedFiles.length >= MAX_HOME_ATTACHMENTS}
            onSelect={() => {
              HomeTelemetry.uploadDocumentSelected();
              fileInputRef.current?.click();
            }}
          >
            <UploadIcon className="mr-2 size-4" />
            Upload document
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuItem
          onSelect={() => {
            HomeTelemetry.recordTaskSelected();
            recordTaskMutation.mutate();
          }}
        >
          <VideoIcon className="mr-2 size-4" />
          Record task
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );

  if (!minimal) {
    return (
      <div className="relative isolate flex flex-col items-center pb-4 pt-12 md:pt-24">
        {/* The artwork is translucent white: it reads as shading on the dark background and is inverted for light. */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[30rem] opacity-85 invert [mask-image:radial-gradient(closest-side,#000_35%,transparent_100%)] dark:opacity-100 dark:invert-0 md:h-[35rem]"
          style={{
            background: `url(${img}) 50% / cover no-repeat`,
          }}
        />
        <div className="flex w-full max-w-[45rem] flex-col items-start text-left md:items-center md:text-center">
          <span className="text-2xl font-semibold tracking-tight md:text-[1.875rem] md:leading-[2.375rem]">
            What task would you like to accomplish?
          </span>
          <p className="mt-2.5 text-[15px] leading-[22px] text-muted-foreground">
            Describe it like you would to a colleague. Skyvern opens a browser
            and does it.
          </p>
        </div>
        <div className="mt-6 flex w-full max-w-[45rem] flex-col md:mt-9">
          <div className="flex w-full flex-col rounded-2xl border border-input bg-background text-muted-foreground shadow-[0_12px_32px_rgba(0,0,0,0.06)] transition-[border-color,box-shadow] focus-within:border-foreground/20 focus-within:shadow-[0_0_0_4px_rgba(79,70,229,0.10),0_12px_32px_rgba(0,0,0,0.06)] dark:bg-slate-elevation1 dark:shadow-[0_12px_32px_rgba(0,0,0,0.35)] dark:focus-within:shadow-[0_0_0_4px_rgba(165,180,252,0.14),0_12px_32px_rgba(0,0,0,0.35)]">
            <CyclingPlaceholderTextarea
              ref={textareaRef}
              id="discover-prompt-input"
              className="max-h-[14rem] min-h-[6rem] resize-none overflow-y-auto border-0 bg-transparent px-5 pb-1.5 pt-[18px] text-base leading-6 text-foreground shadow-none placeholder:text-muted-foreground hover:border-0 focus-visible:ring-0 md:text-[15px]"
              value={prompt}
              onChange={(e) => updatePrompt(e.target.value)}
              onKeyDown={handlePromptKeyDown}
              onFocus={() => setPromptTouched(true)}
              cycling={!promptTouched}
            />
            {enableCopilotHandoff ? (
              <div className="px-5">{attachmentChips}</div>
            ) : null}
            <div className="flex items-center gap-1.5 px-2.5 pb-2.5 pt-2">
              {!hideFlagControls && enableCopilotHandoff ? (
                <>
                  {renderAddMenu("size-11 md:size-9")}
                  {fileInput}
                </>
              ) : null}
              {!hideFlagControls ? (
                <SpeechInputButton
                  isSupported={isSpeechSupported}
                  isListening={isSpeechListening}
                  isHearingSpeech={isSpeechHearing}
                  disabled={isSubmitting}
                  onToggle={() => {
                    HomeTelemetry.voiceToggled();
                    setPromptTouched(true);
                    toggleSpeech();
                  }}
                  className="size-11 rounded-full border-0 bg-transparent md:size-9"
                  iconClassName="h-[18px] w-[18px]"
                />
              ) : null}
              {!hideFlagControls && !enableCopilotHandoff ? (
                <AdvancedSettingsPopover
                  settings={taskRunSettings}
                  onChange={setTaskRunSettings}
                  open={showAdvancedSettings}
                  onOpenChange={(open) => {
                    HomeTelemetry.advancedSettingsToggled(open);
                    setShowAdvancedSettings(open);
                  }}
                  tab={advancedSettingsTab}
                  onTabChange={setAdvancedSettingsTab}
                  triggerClassName="size-11 rounded-full md:size-9"
                />
              ) : null}
              <button
                type="button"
                aria-label="submit-prompt"
                disabled={!prompt.trim() || isSubmitting}
                className="ml-auto flex size-11 shrink-0 items-center justify-center rounded-lg bg-cta text-cta-foreground transition hover:bg-cta-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.92] disabled:pointer-events-none disabled:opacity-50 md:size-9"
                onClick={() => {
                  submitPrompt({ prompt, attribution: exampleAttribution });
                }}
              >
                {isSubmitting ? (
                  <ReloadIcon className="size-4 animate-spin" />
                ) : (
                  <ArrowUpIcon aria-hidden="true" className="size-4" />
                )}
              </button>
            </div>
          </div>
          {!enableCopilotHandoff ? (
            <ChangedSettingsChips
              settings={taskRunSettings}
              onChange={setTaskRunSettings}
              onEdit={(tab) => {
                setAdvancedSettingsTab(tab);
                setShowAdvancedSettings(true);
              }}
            />
          ) : null}
          {secondaryAction ? (
            <div className="mt-3 flex md:justify-end">{secondaryAction}</div>
          ) : null}
        </div>
        <section className="mt-9 w-full max-w-[60rem] md:mt-16">
          <div className="mb-3 flex flex-col gap-1 md:mb-3.5 md:flex-row md:items-baseline md:justify-between">
            <h2 className="text-sm font-semibold">Try an example</h2>
            <p className="text-[13px] text-muted-foreground">
              Pick one to load it into the prompt, edit it, then run.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 sm:gap-3 lg:grid-cols-3">
            {exampleCases.map((example) => (
              <ExampleCasePill
                key={example.key}
                icon={example.icon}
                label={example.label}
                hint={example.hint}
                selected={
                  exampleAttribution?.id === example.key &&
                  !exampleAttribution.edited
                }
                disabled={isSubmitting}
                onClick={() => {
                  HomeTelemetry.exampleClicked({
                    example: example.key,
                    label: example.label,
                  });
                  cancelSpeech();
                  setPrompt(
                    withExampleAttachment(
                      example.prompt,
                      "attachment" in example ? example.attachment : undefined,
                    ),
                  );
                  setExampleAttribution({ id: example.key, edited: false });
                  textareaRef.current?.focus();
                }}
              />
            ))}
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="relative mx-auto flex w-full max-w-2xl flex-col items-center px-4">
      <div
        className={cn("flex flex-col items-center text-center", {
          "mb-6": minimal,
          "mb-7": !minimal,
        })}
      >
        <span className="text-2xl">What task would you like to do?</span>
        {minimal ? (
          <p className="mt-2 max-w-[47rem] text-[13.5px] text-muted-foreground">
            Describe it like you would to a colleague. Skyvern opens a browser
            and does it.
            <button
              type="button"
              aria-expanded={showHowItWorks}
              onClick={() =>
                setShowHowItWorks((value) => {
                  HomeTelemetry.howItWorksToggled(!value);
                  return !value;
                })
              }
              className="ml-1.5 inline-flex items-center gap-1 text-[13px] text-foreground/80 underline decoration-muted-foreground/60 underline-offset-[3px] hover:text-foreground"
            >
              How it works
              <ChevronDownIcon
                aria-hidden="true"
                className={cn("size-3 transition-transform", {
                  "rotate-180": showHowItWorks,
                })}
              />
            </button>
          </p>
        ) : null}
      </div>
      <div className="flex w-full flex-col">
        <div className="flex w-full flex-col rounded-xl border border-input bg-background p-2 text-muted-foreground shadow-sm transition-colors focus-within:border-foreground/20 focus-within:ring-2 focus-within:ring-ring/10">
          <CyclingPlaceholderTextarea
            ref={textareaRef}
            id="discover-prompt-input"
            className="max-h-[8rem] min-h-[4rem] resize-none overflow-y-auto border-0 bg-transparent px-3 py-3 leading-5 text-foreground shadow-none placeholder:text-muted-foreground hover:border-0 focus-visible:ring-0"
            value={prompt}
            onChange={(e) => updatePrompt(e.target.value)}
            onKeyDown={handlePromptKeyDown}
            onFocus={() => setPromptTouched(true)}
            cycling={!promptTouched}
          />
          {attachmentChips}
          <div className="flex items-center gap-1 pt-2">
            {renderAddMenu("size-8")}
            {fileInput}
            <div className="ml-auto flex items-center gap-1.5">
              <SpeechInputButton
                isSupported={isSpeechSupported}
                isListening={isSpeechListening}
                isHearingSpeech={isSpeechHearing}
                disabled={isSubmitting}
                onToggle={() => {
                  HomeTelemetry.voiceToggled();
                  setPromptTouched(true);
                  toggleSpeech();
                }}
                className="h-8 w-8 rounded-full border-0 bg-transparent"
                iconClassName="h-4 w-4"
              />
              <button
                type="button"
                aria-label="submit-prompt"
                disabled={!prompt.trim() || isSubmitting}
                className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-cta text-cta-foreground transition hover:bg-cta-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring active:scale-[0.92] disabled:pointer-events-none disabled:opacity-50"
                onClick={() => {
                  submitPrompt({ prompt, attribution: exampleAttribution });
                }}
              >
                {isSubmitting ? (
                  <ReloadIcon className="size-4 animate-spin" />
                ) : (
                  <ArrowUpIcon aria-hidden="true" className="size-4" />
                )}
              </button>
            </div>
          </div>
        </div>
      </div>
      {minimal ? (
        <div className="mt-[34px] flex w-[76rem] max-w-[calc(100vw-8rem)] flex-col items-center">
          <CapabilityExamples
            disabled={isSubmitting}
            onSelect={(example) => {
              HomeTelemetry.exampleClicked({
                example: example.id,
                capability: example.capability,
                label: example.label,
              });
              cancelSpeech();
              setPrompt(
                withExampleAttachment(example.prompt, example.attachment),
              );
              setExampleAttribution({ id: example.id, edited: false });
              setPromptTouched(true);
              textareaRef.current?.focus();
            }}
            onPreview={(example) =>
              HomeTelemetry.examplePreviewShown({
                example: example.id,
                capability: example.capability,
                label: example.label,
              })
            }
          />
          {showHowItWorks ? (
            <div className="mt-6 flex w-full max-w-[54rem] flex-col gap-3.5 rounded-xl border border-border/70 bg-slate-elevation1/60 px-[18px] py-4 md:flex-row md:items-stretch">
              {HOW_IT_WORKS.map((step, index) => (
                <div key={step.title} className="flex flex-1 gap-3.5">
                  {index > 0 ? (
                    <div
                      aria-hidden="true"
                      className="hidden w-px shrink-0 self-stretch bg-border/70 md:block"
                    />
                  ) : null}
                  <div className="min-w-0">
                    <div className="mb-1.5 text-indigo-300">{step.icon}</div>
                    <h3 className="mb-1 text-[13px] font-semibold">
                      {step.title}
                    </h3>
                    <p className="text-xs leading-normal text-muted-foreground">
                      {step.body}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

const PromptBox = forwardRef(PromptBoxImpl);

export { PromptBox };
export type { ExamplePromptKey, PromptBoxHandle };
