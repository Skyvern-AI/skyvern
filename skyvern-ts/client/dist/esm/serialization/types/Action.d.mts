import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { ActionStatus } from "./ActionStatus.mjs";
import { ActionType } from "./ActionType.mjs";
import { InputOrSelectContext } from "./InputOrSelectContext.mjs";
import { SelectOption } from "./SelectOption.mjs";
import { UserDefinedError } from "./UserDefinedError.mjs";
export declare const Action: core.serialization.ObjectSchema<serializers.Action.Raw, Skyvern.Action>;
export declare namespace Action {
    interface Raw {
        action_type: ActionType.Raw;
        status?: ActionStatus.Raw | null;
        action_id?: string | null;
        source_action_id?: string | null;
        organization_id?: string | null;
        workflow_run_id?: string | null;
        task_id?: string | null;
        step_id?: string | null;
        step_order?: number | null;
        action_order?: number | null;
        confidence_float?: number | null;
        description?: string | null;
        reasoning?: string | null;
        intention?: string | null;
        response?: string | null;
        element_id?: string | null;
        skyvern_element_hash?: string | null;
        skyvern_element_data?: Record<string, unknown> | null;
        tool_call_id?: string | null;
        xpath?: string | null;
        errors?: UserDefinedError.Raw[] | null;
        data_extraction_goal?: string | null;
        file_name?: string | null;
        file_url?: string | null;
        download?: boolean | null;
        is_upload_file_tag?: boolean | null;
        text?: string | null;
        input_or_select_context?: InputOrSelectContext.Raw | null;
        option?: SelectOption.Raw | null;
        is_checked?: boolean | null;
        verified?: boolean | null;
        totp_timing_info?: Record<string, unknown> | null;
        created_at?: string | null;
        modified_at?: string | null;
        created_by?: string | null;
    }
}
