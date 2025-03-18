import { getClient } from "@/api/AxiosClient";
import {
  Createv2TaskRequest,
  TaskV2,
  ProxyLocation,
  TaskGenerationApiResponse,
} from "@/api/types";
import img from "@/assets/promptBoxBg.png";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { CartIcon } from "@/components/icons/CartIcon";
import { GraphIcon } from "@/components/icons/GraphIcon";
import { InboxIcon } from "@/components/icons/InboxIcon";
import { MessageIcon } from "@/components/icons/MessageIcon";
import { TrophyIcon } from "@/components/icons/TrophyIcon";
import { ProxySelector } from "@/components/ProxySelector";
import { Input } from "@/components/ui/input";
import {
  CustomSelectItem,
  Select,
  SelectContent,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import {
  FileTextIcon,
  GearIcon,
  PaperPlaneIcon,
  Pencil1Icon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import {
  generatePhoneNumber,
  generateUniqueEmail,
} from "../data/sampleTaskData";
import { ExampleCasePill } from "./ExampleCasePill";
import { MAX_STEPS_DEFAULT } from "@/routes/workflows/editor/nodes/Taskv2Node/types";

function createTemplateTaskFromTaskGenerationParameters(
  values: TaskGenerationApiResponse,
) {
  return {
    title: values.suggested_title ?? "Untitled Task",
    description: "",
    is_saved_task: true,
    webhook_callback_url: null,
    proxy_location: "RESIDENTIAL",
    workflow_definition: {
      parameters: [
        {
          parameter_type: "workflow",
          workflow_parameter_type: "json",
          key: "navigation_payload",
          default_value: JSON.stringify(values.navigation_payload),
        },
      ],
      blocks: [
        {
          block_type: "task",
          label: values.suggested_title ?? "Untitled Task",
          url: values.url,
          navigation_goal: values.navigation_goal,
          data_extraction_goal: values.data_extraction_goal,
          data_schema: values.extracted_information_schema,
        },
      ],
    },
  };
}

const exampleCases = [
  {
    key: "finditparts",
    label: "Add a product to cart",
    prompt:
      'Go to https://www.finditparts.com first. Search for the product "W01-377-8537", add it to cart and then navigate to the cart page. Your goal is COMPLETE when you\'re on the cart page and the specified product is in the cart. Extract all product quantity information from the cart page. Do not attempt to checkout.',
    icon: <CartIcon className="size-6" />,
  },
  {
    key: "job_application",
    label: "Apply for a job",
    prompt: `Go to https://jobs.lever.co/leverdemo-8/45d39614-464a-4b62-a5cd-8683ce4fb80a/apply, fill out the job application form and apply to the job. Fill out any public burden questions if they appear in the form. Your goal is complete when the page says you've successfully applied to the job. Terminate if you are unable to apply successfully. Here's the user information: {"name":"John Doe","email":"${generateUniqueEmail()}","phone":"${generatePhoneNumber()}","resume_url":"https://writing.colostate.edu/guides/documents/resume/functionalSample.pdf","cover_letter":"Generate a compelling cover letter for me"}`,
    icon: <InboxIcon className="size-6" />,
  },
  {
    key: "geico",
    label: "Get an insurance quote",
    prompt: `Go to https://www.geico.com first. Navigate through the website until you generate an auto insurance quote. Do not generate a home insurance quote. If you're on a page showing an auto insurance quote (with premium amounts), your goal is COMPLETE. Extract all quote information in JSON format including the premium amount, the timeframe for the quote. Here's the user information: {"licensed_at_age":19,"education_level":"HIGH_SCHOOL","phone_number":"8042221111","full_name":"Chris P. Bacon","past_claim":[],"has_claims":false,"spouse_occupation":"Florist","auto_current_carrier":"None","home_commercial_uses":null,"spouse_full_name":"Amy Stake","auto_commercial_uses":null,"requires_sr22":false,"previous_address_move_date":null,"line_of_work":null,"spouse_age":"1987-12-12","auto_insurance_deadline":null,"email":"chris.p.bacon@abc.com","net_worth_numeric":1000000,"spouse_gender":"F","marital_status":"married","spouse_licensed_at_age":20,"license_number":"AAAAAAA090AA","spouse_license_number":"AAAAAAA080AA","how_much_can_you_lose":25000,"vehicles":[{"annual_mileage":10000,"commute_mileage":4000,"existing_coverages":null,"ideal_coverages":{"bodily_injury_per_incident_limit":50000,"bodily_injury_per_person_limit":25000,"collision_deductible":1000,"comprehensive_deductible":1000,"personal_injury_protection":null,"property_damage_per_incident_limit":null,"property_damage_per_person_limit":25000,"rental_reimbursement_per_incident_limit":null,"rental_reimbursement_per_person_limit":null,"roadside_assistance_limit":null,"underinsured_motorist_bodily_injury_per_incident_limit":50000,"underinsured_motorist_bodily_injury_per_person_limit":25000,"underinsured_motorist_property_limit":null},"ownership":"Owned","parked":"Garage","purpose":"commute","vehicle":{"style":"AWD 3.0 quattro TDI 4dr Sedan","model":"A8 L","price_estimate":29084,"year":2015,"make":"Audi"},"vehicle_id":null,"vin":null}],"additional_drivers":[],"home":[{"home_ownership":"owned"}],"spouse_line_of_work":"Agriculture, Forestry and Fishing","occupation":"Customer Service Representative","id":null,"gender":"M","credit_check_authorized":false,"age":"1987-11-11","license_state":"Washington","cash_on_hand":"$10000–14999","address":{"city":"HOUSTON","country":"US","state":"TX","street":"9625 GARFIELD AVE.","zip":"77082"},"spouse_education_level":"MASTERS","spouse_email":"amy.stake@abc.com","spouse_added_to_auto_policy":true}`,
    icon: <FileTextIcon className="size-6" />,
  },
  {
    key: "california_edd",
    label: "Fill out CA's online EDD",
    prompt: `Go to https://eddservices.edd.ca.gov/acctservices/AccountManagement/AccountServlet?Command=NEW_SIGN_UP. Navigate through the employer services online enrollment form. Terminate when the form is completed. Here's the needed information: {"username":"isthisreal1","password":"Password123!","first_name":"John","last_name":"Doe","pin":"1234","email":"${generateUniqueEmail()}","phone_number":"${generatePhoneNumber()}"}`,
    icon: <Pencil1Icon className="size-6" />,
  },
  {
    key: "contact_us_forms",
    label: "Fill a contact us form",
    prompt: `Go to https://canadahvac.com/contact-hvac-canada. Fill out the contact us form and submit it. Your goal is complete when the page says your message has been sent. Here's the user information: {"name":"John Doe","email":"john.doe@gmail.com","phone":"123-456-7890","message":"Hello, I have a question about your services."}`,
    icon: <FileTextIcon className="size-6" />,
  },
  {
    key: "hackernews",
    label: "What's the top post on hackernews",
    prompt: "Navigate to the Hacker News homepage and get the top 3 posts.",
    icon: <MessageIcon className="size-6" />,
  },
  {
    key: "AAPLStockPrice",
    label: "Search for AAPL on Google Finance",
    prompt:
      'Go to google finance and find the "AAPL" stock price. COMPLETE when the search results for "AAPL" are displayed and the stock price is extracted.',
    icon: <GraphIcon className="size-6" />,
  },
  {
    key: "topRankedFootballTeam",
    label: "Get the top ranked football team",
    prompt:
      "Navigate to the FIFA World Ranking page and identify the top ranked football team. Extract the name of the top ranked football team from the FIFA World Ranking page.",
    icon: <TrophyIcon className="size-6" />,
  },
  {
    key: "extractIntegrationsFromGong",
    label: "Extract Integrations from Gong.io",
    prompt:
      "Go to https://www.gong.io first. Navigate to the 'Integrations' page on the Gong website. Extract the names and descriptions of all integrations listed on the Gong integrations page. Ensure not to click on any external links or advertisements.",
    icon: <GearIcon className="size-6" />,
  },
];

function PromptBox() {
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState<string>("");
  const [selectValue, setSelectValue] = useState<"v1" | "v2">("v2"); // Observer is the default
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [webhookCallbackUrl, setWebhookCallbackUrl] = useState<string | null>(
    null,
  );
  const [proxyLocation, setProxyLocation] = useState<ProxyLocation>(
    ProxyLocation.Residential,
  );
  const [publishWorkflow, setPublishWorkflow] = useState(false);
  const [totpIdentifier, setTotpIdentifier] = useState("");
  const [maxStepsOverride, setMaxStepsOverride] = useState<string | null>(null);
  const [showAdvancedSettings, setShowAdvancedSettings] = useState(false);
  const [dataSchema, setDataSchema] = useState<string | null>(null);

  const startObserverCruiseMutation = useMutation({
    mutationFn: async (prompt: string) => {
      const client = await getClient(credentialGetter, "v2");
      return client.post<Createv2TaskRequest, { data: TaskV2 }>(
        "/tasks",
        {
          user_prompt: prompt,
          webhook_callback_url: webhookCallbackUrl,
          proxy_location: proxyLocation,
          totp_identifier: totpIdentifier,
          publish_workflow: publishWorkflow,
          extracted_information_schema: dataSchema
            ? (() => {
                try {
                  return JSON.parse(dataSchema);
                } catch (e) {
                  return dataSchema;
                }
              })()
            : null,
        },
        {
          headers: {
            "x-max-steps-override": maxStepsOverride,
          },
        },
      );
    },
    onSuccess: (response) => {
      toast({
        variant: "success",
        title: "Workflow Run Created",
        description: `Workflow run created successfully.`,
      });
      queryClient.invalidateQueries({
        queryKey: ["workflowRuns"],
      });
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["runs"],
      });
      navigate(
        `/workflows/${response.data.workflow_permanent_id}/${response.data.workflow_run_id}`,
      );
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Error creating workflow run from prompt",
        description: error.message,
      });
    },
  });

  const getTaskFromPromptMutation = useMutation({
    mutationFn: async (prompt: string) => {
      const client = await getClient(credentialGetter);
      return client
        .post<
          { prompt: string },
          { data: TaskGenerationApiResponse }
        >("/generate/task", { prompt })
        .then((response) => response.data);
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        variant: "destructive",
        title: "Error creating task from prompt",
        description: detail ? detail : error.message,
      });
    },
  });

  const saveTaskMutation = useMutation({
    mutationFn: async (params: TaskGenerationApiResponse) => {
      const client = await getClient(credentialGetter);
      const templateTask =
        createTemplateTaskFromTaskGenerationParameters(params);
      const yaml = convertToYAML(templateTask);
      return client.post("/workflows", yaml, {
        headers: {
          "Content-Type": "text/plain",
        },
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["savedTasks"],
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Error saving task",
        description: error.message,
      });
    },
  });

  return (
    <div>
      <div
        className="rounded-sm py-[4.25rem]"
        style={{
          background: `url(${img}) 50% / cover no-repeat`,
        }}
      >
        <div className="mx-auto flex min-w-44 flex-col items-center gap-7 px-8">
          <span className="text-2xl">
            What task would you like to accomplish?
          </span>
          <div className="flex w-full max-w-xl flex-col">
            <div className="flex w-full items-center gap-2 rounded-xl bg-slate-700 py-2 pr-4">
              <AutoResizingTextarea
                className="min-h-0 resize-none rounded-xl border-transparent px-4 hover:border-transparent focus-visible:ring-0"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="Enter your prompt..."
              />
              <Select
                value={selectValue}
                onValueChange={(value: "v1" | "v2") => {
                  setSelectValue(value);
                }}
              >
                <SelectTrigger className="w-48 focus:ring-0">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="border-slate-500 bg-slate-elevation3">
                  <CustomSelectItem value="v1">
                    <div className="space-y-2">
                      <div>
                        <SelectItemText>Skyvern 1.0</SelectItemText>
                      </div>
                      <div className="text-xs text-slate-400">
                        Best for simple tasks
                      </div>
                    </div>
                  </CustomSelectItem>
                  <CustomSelectItem value="v2" className="hover:bg-slate-800">
                    <div className="space-y-2">
                      <div>
                        <SelectItemText>Skyvern 2.0</SelectItemText>
                      </div>
                      <div className="text-xs text-slate-400">
                        Best for complex tasks
                      </div>
                    </div>
                  </CustomSelectItem>
                </SelectContent>
              </Select>
              <div className="flex items-center">
                <GearIcon
                  className="size-6 cursor-pointer"
                  onClick={() => {
                    setShowAdvancedSettings((value) => !value);
                  }}
                />
              </div>
              <div className="flex items-center">
                {startObserverCruiseMutation.isPending ||
                getTaskFromPromptMutation.isPending ||
                saveTaskMutation.isPending ? (
                  <ReloadIcon className="size-6 animate-spin" />
                ) : (
                  <PaperPlaneIcon
                    className="size-6 cursor-pointer"
                    onClick={async () => {
                      if (selectValue === "v2") {
                        startObserverCruiseMutation.mutate(prompt);
                        return;
                      }
                      const taskGenerationResponse =
                        await getTaskFromPromptMutation.mutateAsync(prompt);
                      await saveTaskMutation.mutateAsync(
                        taskGenerationResponse,
                      );
                      navigate("/tasks/create/from-prompt", {
                        state: {
                          data: taskGenerationResponse,
                        },
                      });
                    }}
                  />
                )}
              </div>
            </div>
            {showAdvancedSettings ? (
              <div className="rounded-b-lg px-2">
                <div className="space-y-4 rounded-b-xl bg-slate-900 p-4">
                  <header>Advanced Settings</header>
                  <div className="flex gap-16">
                    <div className="w-48 shrink-0">
                      <div className="text-sm">Webhook Callback URL</div>
                      <div className="text-xs text-slate-400">
                        The URL of a webhook endpoint to send the extracted
                        information
                      </div>
                    </div>
                    <Input
                      value={webhookCallbackUrl ?? ""}
                      onChange={(event) => {
                        setWebhookCallbackUrl(event.target.value);
                      }}
                    />
                  </div>
                  <div className="flex gap-16">
                    <div className="w-48 shrink-0">
                      <div className="text-sm">Proxy Location</div>
                      <div className="text-xs text-slate-400">
                        Route Skyvern through one of our available proxies.
                      </div>
                    </div>
                    <ProxySelector
                      value={proxyLocation}
                      onChange={setProxyLocation}
                    />
                  </div>
                  <div className="flex gap-16">
                    <div className="w-48 shrink-0">
                      <div className="text-sm">2FA Identifier</div>
                      <div className="text-xs text-slate-400">
                        The identifier for a 2FA code for this task.
                      </div>
                    </div>
                    <Input
                      value={totpIdentifier}
                      onChange={(event) => {
                        setTotpIdentifier(event.target.value);
                      }}
                    />
                  </div>
                  <div className="flex gap-16">
                    <div className="w-48 shrink-0">
                      <div className="text-sm">Publish Workflow</div>
                      <div className="text-xs text-slate-400">
                        Whether to create a workflow alongside this task run.
                      </div>
                    </div>
                    <Switch
                      checked={publishWorkflow}
                      onCheckedChange={(checked) => {
                        setPublishWorkflow(Boolean(checked));
                      }}
                    />
                  </div>
                  <div className="flex gap-16">
                    <div className="w-48 shrink-0">
                      <div className="text-sm">Max Steps Override</div>
                      <div className="text-xs text-slate-400">
                        The maximum number of steps to take for this task.
                      </div>
                    </div>
                    <Input
                      value={maxStepsOverride ?? ""}
                      placeholder={`Default: ${MAX_STEPS_DEFAULT}`}
                      onChange={(event) => {
                        setMaxStepsOverride(event.target.value);
                      }}
                    />
                  </div>
                  <div className="flex gap-16">
                    <div className="w-48 shrink-0">
                      <div className="text-sm">Data Schema</div>
                      <div className="text-xs text-slate-400">
                        Specify the output data schema in JSON format
                      </div>
                    </div>
                    <div className="flex-1">
                      <CodeEditor
                        value={dataSchema ?? ""}
                        onChange={(value) => setDataSchema(value || null)}
                        language="json"
                        minHeight="100px"
                        maxHeight="500px"
                        fontSize={8}
                      />
                    </div>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      </div>
      <div className="flex flex-wrap justify-center gap-4 rounded-sm bg-slate-elevation1 p-4">
        {exampleCases.map((example) => {
          return (
            <ExampleCasePill
              key={example.key}
              exampleId={example.key}
              icon={example.icon}
              label={example.label}
              prompt={example.prompt}
              version={selectValue}
            />
          );
        })}
      </div>
    </div>
  );
}

export { PromptBox };
