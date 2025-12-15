import { getClient } from "@/api/AxiosClient";
import {
  CreateTaskRequest,
  OrganizationApiResponse,
  ProxyLocation,
  RunEngine,
} from "@/api/types";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Button } from "@/components/ui/button";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { toast } from "@/components/ui/use-toast";
import { KeyValueInput } from "@/components/KeyValueInput";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { runsApiBaseUrl } from "@/util/env";
import { CopyApiCommandDropdown } from "@/components/CopyApiCommandDropdown";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { buildTaskRunPayload } from "@/util/taskRunPayload";
import { zodResolver } from "@hookform/resolvers/zod";
import { PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { ToastAction } from "@radix-ui/react-toast";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { useState } from "react";
import { useForm, useFormState } from "react-hook-form";
import { Link } from "react-router-dom";
import { MAX_STEPS_DEFAULT } from "../constants";
import { TaskFormSection } from "./TaskFormSection";
import {
  createNewTaskFormSchema,
  CreateNewTaskFormValues,
} from "./taskFormTypes";
import { ProxySelector } from "@/components/ProxySelector";
import { Switch } from "@/components/ui/switch";
import { MAX_SCREENSHOT_SCROLLS_DEFAULT } from "@/routes/workflows/editor/nodes/Taskv2Node/types";
import { TestWebhookDialog } from "@/components/TestWebhookDialog";
type Props = {
  initialValues: CreateNewTaskFormValues;
};

function transform<T>(value: T): T | null {
  return value === "" ? null : value;
}

function createTaskRequestObject(
  formValues: CreateNewTaskFormValues,
): CreateTaskRequest {
  let extractedInformationSchema = null;
  if (formValues.extractedInformationSchema) {
    try {
      extractedInformationSchema = JSON.parse(
        formValues.extractedInformationSchema,
      );
    } catch (e) {
      extractedInformationSchema = formValues.extractedInformationSchema;
    }
  }
  let extraHttpHeaders = null;
  if (formValues.extraHttpHeaders) {
    try {
      extraHttpHeaders = JSON.parse(formValues.extraHttpHeaders);
    } catch (e) {
      extraHttpHeaders = formValues.extraHttpHeaders;
    }
  }
  let errorCodeMapping = null;
  if (formValues.errorCodeMapping) {
    try {
      errorCodeMapping = JSON.parse(formValues.errorCodeMapping);
    } catch (e) {
      errorCodeMapping = formValues.errorCodeMapping;
    }
  }

  return {
    title: null,
    url: formValues.url,
    webhook_callback_url: transform(formValues.webhookCallbackUrl),
    navigation_goal: transform(formValues.navigationGoal),
    data_extraction_goal: transform(formValues.dataExtractionGoal),
    proxy_location: formValues.proxyLocation ?? ProxyLocation.Residential,
    navigation_payload: transform(formValues.navigationPayload),
    extracted_information_schema: extractedInformationSchema,
    extra_http_headers: extraHttpHeaders,
    totp_identifier: transform(formValues.totpIdentifier),
    browser_address: transform(formValues.cdpAddress),
    error_code_mapping: errorCodeMapping,
    max_screenshot_scrolls: formValues.maxScreenshotScrolls,
    include_action_history_in_verification:
      formValues.includeActionHistoryInVerification,
  };
}

type Section = "base" | "extraction" | "advanced";

function CreateNewTaskForm({ initialValues }: Props) {
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
  const [activeSections, setActiveSections] = useState<Array<Section>>([
    "base",
  ]);
  const [showAdvancedBaseContent, setShowAdvancedBaseContent] = useState(false);

  const { data: organizations } = useQuery<Array<OrganizationApiResponse>>({
    queryKey: ["organizations"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return await client
        .get("/organizations")
        .then((response) => response.data.organizations);
    },
  });

  const organization = organizations?.[0];

  const form = useForm<CreateNewTaskFormValues>({
    resolver: zodResolver(createNewTaskFormSchema),
    defaultValues: {
      ...initialValues,
      maxStepsOverride: initialValues.maxStepsOverride ?? null,
      proxyLocation: initialValues.proxyLocation ?? ProxyLocation.Residential,
      maxScreenshotScrolls: initialValues.maxScreenshotScrolls ?? null,
      cdpAddress: initialValues.cdpAddress ?? null,
    },
  });
  const { errors } = useFormState({ control: form.control });

  const mutation = useMutation({
    mutationFn: async (formValues: CreateNewTaskFormValues) => {
      const taskRequest = createTaskRequestObject(formValues);
      const client = await getClient(credentialGetter);
      const includeOverrideHeader =
        formValues.maxStepsOverride !== null &&
        formValues.maxStepsOverride !== MAX_STEPS_DEFAULT;
      return client.post<
        ReturnType<typeof createTaskRequestObject>,
        { data: { task_id: string } }
      >("/tasks", taskRequest, {
        ...(includeOverrideHeader && {
          headers: {
            "x-max-steps-override": formValues.maxStepsOverride,
          },
        }),
      });
    },
    onError: (error: AxiosError) => {
      if (error.response?.status === 402) {
        toast({
          variant: "destructive",
          title: "작업 생성 실패",
          description:
            "이 작업을 실행할 크레딧이 부족합니다. 결제 페이지에서 크레딧 잔액을 확인하세요.",
          action: (
            <ToastAction altText="결제 페이지로">
              <Button asChild>
                <Link to="billing">결제 페이지로</Link>
              </Button>
            </ToastAction>
          ),
        });
        return;
      }
      toast({
        variant: "destructive",
        title: "작업 생성 중 오류가 발생했습니다.",
        description: error.message,
      });
    },
    onSuccess: (response) => {
      toast({
        variant: "success",
        title: "작업 생성됨",
        description: `${response.data.task_id}가 성공적으로 생성되었습니다.`,
        action: (
          <ToastAction altText="보기">
            <Button asChild>
              <Link to={`/tasks/${response.data.task_id}`}>보기</Link>
            </Button>
          </ToastAction>
        ),
      });
      queryClient.invalidateQueries({
        queryKey: ["tasks"],
      });
      queryClient.invalidateQueries({
        queryKey: ["runs"],
      });
    },
  });

  function onSubmit(values: CreateNewTaskFormValues) {
    mutation.mutate(values);
  }

  function isActive(section: Section) {
    return activeSections.includes(section);
  }

  function toggleSection(section: Section) {
    if (isActive(section)) {
      setActiveSections(activeSections.filter((s) => s !== section));
    } else {
      setActiveSections([...activeSections, section]);
    }
  }

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
        <TaskFormSection
          index={1}
          title="기본 설정"
          active={isActive("base")}
          onClick={() => {
            toggleSection("base");
          }}
          hasError={
            typeof errors.url !== "undefined" ||
            typeof errors.navigationGoal !== "undefined"
          }
        >
          {isActive("base") && (
            <div className="space-y-6">
              <div className="space-y-4">
                <FormField
                  control={form.control}
                  name="url"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">URL</h1>
                            <h2 className="text-base text-slate-400">
                              작업의 시작 URL
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input placeholder="https://" {...field} />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="navigationGoal"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">탐색 목표</h1>
                            <h2 className="text-base text-slate-400">
                              어디로 이동하고 무엇을 수행해야 하나요?
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <AutoResizingTextarea
                              {...field}
                              placeholder="수행할 작업을 입력하세요."
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                {showAdvancedBaseContent ? (
                  <div className="border-t border-dashed pt-4">
                    <FormField
                      control={form.control}
                      name="navigationPayload"
                      render={({ field }) => (
                        <FormItem>
                          <div className="flex gap-16">
                            <FormLabel>
                              <div className="w-72">
                                <h1 className="text-lg">탐색 페이로드</h1>
                                <h2 className="text-base text-slate-400">
                                  중요한 매개변수, 경로 또는 상태 지정
                                </h2>
                              </div>
                              <Button
                                className="mt-4"
                                type="button"
                                variant="tertiary"
                                onClick={() => {
                                  setShowAdvancedBaseContent(false);
                                }}
                                size="sm"
                              >
                                고급 설정 숨기기
                              </Button>
                            </FormLabel>
                            <div className="w-full">
                              <FormControl>
                                <CodeEditor
                                  {...field}
                                  language="json"
                                  minHeight="96px"
                                  maxHeight="500px"
                                  value={
                                    field.value === null ? "" : field.value
                                  }
                                />
                              </FormControl>
                              <FormMessage />
                            </div>
                          </div>
                        </FormItem>
                      )}
                    />
                  </div>
                ) : (
                  <div>
                    <Button
                      type="button"
                      variant="tertiary"
                      onClick={() => {
                        setShowAdvancedBaseContent(true);
                      }}
                      size="sm"
                    >
                      고급 설정 보기
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}
        </TaskFormSection>
        <TaskFormSection
          index={2}
          title="데이터 추출"
          active={isActive("extraction")}
          onClick={() => {
            toggleSection("extraction");
          }}
          hasError={
            typeof errors.dataExtractionGoal !== "undefined" ||
            typeof errors.extractedInformationSchema !== "undefined"
          }
        >
          {isActive("extraction") && (
            <div className="space-y-6">
              <div className="space-y-4">
                <FormField
                  control={form.control}
                  name="dataExtractionGoal"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">데이터 추출 목표</h1>
                            <h2 className="text-base text-slate-400">
                              어떤 결과를 얻고 싶으신가요?
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <AutoResizingTextarea
                              {...field}
                              placeholder="추출할 데이터를 설명하세요."
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="extractedInformationSchema"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">데이터 스키마</h1>
                            <h2 className="text-base text-slate-400">
                              JSON 형식으로 출력 형식 지정
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <CodeEditor
                              {...field}
                              language="json"
                              minHeight="96px"
                              maxHeight="500px"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
              </div>
            </div>
          )}
        </TaskFormSection>
        <TaskFormSection
          index={3}
          title="고급 설정"
          active={isActive("advanced")}
          onClick={() => {
            toggleSection("advanced");
          }}
          hasError={
            typeof errors.navigationPayload !== "undefined" ||
            typeof errors.maxStepsOverride !== "undefined" ||
            typeof errors.webhookCallbackUrl !== "undefined" ||
            typeof errors.errorCodeMapping !== "undefined"
          }
        >
          {isActive("advanced") && (
            <div className="space-y-6">
              <div className="space-y-4">
                <FormField
                  control={form.control}
                  name="includeActionHistoryInVerification"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">작업 기록 포함</h1>
                            <h2 className="text-base text-slate-400">
                              작업 완료 확인 시 작업 기록을 포함할지 여부
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Switch
                              checked={field.value ?? false}
                              onCheckedChange={(checked) => {
                                field.onChange(checked);
                              }}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="maxStepsOverride"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">최대 단계 수 설정</h1>
                            <h2 className="text-base text-slate-400">
                              기본값보다 더 많거나 적은 단계를 실행하시겠습니까?
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              type="number"
                              min={1}
                              value={field.value ?? ""}
                              placeholder={`기본값: ${organization?.max_steps_per_run ?? MAX_STEPS_DEFAULT}`}
                              onChange={(event) => {
                                const value =
                                  event.target.value === ""
                                    ? null
                                    : Number(event.target.value);
                                field.onChange(value);
                              }}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="webhookCallbackUrl"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">웹훅 콜백 URL</h1>
                            <h2 className="text-base text-slate-400">
                              추출된 정보를 전송할 웹훅 엔드포인트 URL
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <div className="flex flex-col gap-2">
                              <Input
                                className="w-full"
                                {...field}
                                placeholder="https://"
                                value={field.value === null ? "" : field.value}
                              />
                              <TestWebhookDialog
                                runType="task"
                                runId={null}
                                initialWebhookUrl={
                                  field.value === null ? undefined : field.value
                                }
                                trigger={
                                  <Button
                                    type="button"
                                    variant="secondary"
                                    className="self-start"
                                    disabled={!field.value}
                                  >
                                    웹훅 테스트
                                  </Button>
                                }
                              />
                            </div>
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="proxyLocation"
                  render={({ field }) => {
                    return (
                      <FormItem>
                        <div className="flex gap-16">
                          <FormLabel>
                            <div className="w-72">
                              <div className="flex items-center gap-2 text-lg">
                                프록시 위치
                              </div>
                              <h2 className="text-sm text-slate-400">
                                사용 가능한 프록시 중 하나를 통해 라우팅합니다.
                              </h2>
                            </div>
                          </FormLabel>
                          <div className="w-full space-y-2">
                            <FormControl>
                              <ProxySelector
                                value={field.value}
                                onChange={field.onChange}
                                className="w-48"
                              />
                            </FormControl>
                            <FormMessage />
                          </div>
                        </div>
                      </FormItem>
                    );
                  }}
                />
                <FormField
                  control={form.control}
                  name="maxScreenshotScrolls"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">최대 스크린샷 스크롤</h1>
                            <h2 className="text-base text-slate-400">
                              {`작업 후 스크린샷의 최대 스크롤 횟수. 기본값은 ${MAX_SCREENSHOT_SCROLLS_DEFAULT}입니다. 0으로 설정하면 현재 뷰포트만 캡처합니다.`}
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              type="number"
                              min={0}
                              value={field.value ?? ""}
                              placeholder={`기본값: ${MAX_SCREENSHOT_SCROLLS_DEFAULT}`}
                              onChange={(event) => {
                                const value =
                                  event.target.value === ""
                                    ? null
                                    : Number(event.target.value);
                                field.onChange(value);
                              }}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <Separator />
                <FormField
                  control={form.control}
                  name="extraHttpHeaders"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">추가 HTTP 헤더</h1>
                            <h2 className="text-base text-slate-400">
                              사전 형식으로 사용자 정의 HTTP 요청 헤더를
                              지정하세요
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <KeyValueInput
                              value={field.value ?? ""}
                              onChange={(val) => field.onChange(val)}
                              addButtonText="헤더 추가"
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="errorCodeMapping"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">오류 메시지</h1>
                            <h2 className="text-base text-slate-400">
                              알림 받고 싶은 오류 출력을
                              지정하세요
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <CodeEditor
                              {...field}
                              language="json"
                              minHeight="96px"
                              maxHeight="500px"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <Separator />
                <FormField
                  control={form.control}
                  name="totpIdentifier"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">2FA 식별자</h1>
                            <h2 className="text-base text-slate-400">작업에 연결할 TOTP 식별자</h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              placeholder="작업에 연결할 TOTP ID를 입력하세요"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="cdpAddress"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">브라우저 주소</h1>
                            <h2 className="text-base text-slate-400">
                              작업 실행에 사용할 브라우저 서버
                              주소입니다.
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              placeholder="http://127.0.0.1:9222"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
              </div>
            </div>
          )}
        </TaskFormSection>

        <div className="flex justify-end gap-3">
          <CopyApiCommandDropdown
            getOptions={() => {
              const formValues = form.getValues();
              const includeOverrideHeader =
                formValues.maxStepsOverride !== null &&
                formValues.maxStepsOverride !== MAX_STEPS_DEFAULT;

              const headers: Record<string, string> = {
                "Content-Type": "application/json",
                "x-api-key": apiCredential ?? "<your-api-key>",
              };

              if (includeOverrideHeader) {
                headers["x-max-steps-override"] = String(
                  formValues.maxStepsOverride,
                );
              }

              return {
                method: "POST",
                url: `${runsApiBaseUrl}/run/tasks`,
                body: buildTaskRunPayload(
                  createTaskRequestObject(formValues),
                  RunEngine.SkyvernV1,
                ),
                headers,
              } satisfies ApiCommandOptions;
            }}
          />
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending && (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            )}
            <PlayIcon className="mr-2 h-4 w-4" />
            실행
          </Button>
        </div>
      </form>
    </Form>
  );
}

export { CreateNewTaskForm };
