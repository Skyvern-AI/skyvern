import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ActionType: core.serialization.Schema<serializers.ActionType.Raw, Skyvern.ActionType>;
export declare namespace ActionType {
    type Raw = "click" | "input_text" | "upload_file" | "download_file" | "select_option" | "checkbox" | "wait" | "null_action" | "solve_captcha" | "terminate" | "complete" | "reload_page" | "extract" | "verification_code" | "goto_url" | "scroll" | "keypress" | "move" | "drag" | "left_mouse";
}
