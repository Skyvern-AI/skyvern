import type { WorkflowCreateYAMLRequest } from "./types/workflowYamlTypes";
import type { WorkflowApiResponse } from "./types/workflowTypes";
import { defaultWorkflowRequest } from "./defaultWorkflowRequest";

const DRAFT_WORKFLOW_PERMANENT_ID = "new";

type DraftWorkflowOverrides = Partial<
  Pick<WorkflowCreateYAMLRequest, "title" | "run_with" | "folder_id">
>;

function isDraftWorkflowPermanentId(
  workflowPermanentId: string | undefined,
): workflowPermanentId is typeof DRAFT_WORKFLOW_PERMANENT_ID {
  return workflowPermanentId === DRAFT_WORKFLOW_PERMANENT_ID;
}

function omitUndefinedOverrides(
  overrides: DraftWorkflowOverrides,
): DraftWorkflowOverrides {
  return Object.fromEntries(
    Object.entries(overrides).filter(([, value]) => value !== undefined),
  ) as DraftWorkflowOverrides;
}

function buildDraftWorkflowApiResponse(
  overrides: DraftWorkflowOverrides = {},
): WorkflowApiResponse {
  const request: WorkflowCreateYAMLRequest = {
    ...defaultWorkflowRequest,
    ...omitUndefinedOverrides(overrides),
  };
  const now = new Date().toISOString();

  return {
    workflow_id: DRAFT_WORKFLOW_PERMANENT_ID,
    organization_id: "",
    is_saved_task: false,
    is_template: false,
    title: request.title,
    workflow_permanent_id: DRAFT_WORKFLOW_PERMANENT_ID,
    version: 1,
    description: request.description ?? "",
    workflow_definition: {
      version: request.workflow_definition.version,
      blocks: request.workflow_definition
        .blocks as WorkflowApiResponse["workflow_definition"]["blocks"],
      parameters: request.workflow_definition
        .parameters as WorkflowApiResponse["workflow_definition"]["parameters"],
    },
    proxy_location: null,
    webhook_callback_url: null,
    extra_http_headers: null,
    cdp_connect_headers: null,
    persist_browser_session: false,
    browser_profile_id: null,
    model: null,
    totp_verification_url: null,
    totp_identifier: null,
    max_screenshot_scrolls: null,
    max_elapsed_time_minutes: null,
    status: "draft",
    created_at: now,
    modified_at: now,
    deleted_at: null,
    run_with: request.run_with ?? "agent",
    cache_key: null,
    ai_fallback: request.ai_fallback ?? true,
    adaptive_caching: null,
    code_version: request.code_version ?? null,
    run_sequentially: false,
    sequential_key: null,
    folder_id: request.folder_id ?? null,
    import_error: null,
  };
}

export {
  DRAFT_WORKFLOW_PERMANENT_ID,
  buildDraftWorkflowApiResponse,
  isDraftWorkflowPermanentId,
  type DraftWorkflowOverrides,
};
