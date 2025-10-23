import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { ActionStatus } from "./ActionStatus.js";
import { ActionType } from "./ActionType.js";
import { InputOrSelectContext } from "./InputOrSelectContext.js";
import { SelectOption } from "./SelectOption.js";
import { UserDefinedError } from "./UserDefinedError.js";
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
