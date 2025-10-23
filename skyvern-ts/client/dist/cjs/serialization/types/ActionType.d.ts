import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const ActionType: core.serialization.Schema<serializers.ActionType.Raw, Skyvern.ActionType>;
export declare namespace ActionType {
    type Raw = "click" | "input_text" | "upload_file" | "download_file" | "select_option" | "checkbox" | "wait" | "null_action" | "solve_captcha" | "terminate" | "complete" | "reload_page" | "extract" | "verification_code" | "goto_url" | "scroll" | "keypress" | "move" | "drag" | "left_mouse";
}
