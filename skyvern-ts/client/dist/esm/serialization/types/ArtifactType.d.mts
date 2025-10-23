import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
export declare const ArtifactType: core.serialization.Schema<serializers.ArtifactType.Raw, Skyvern.ArtifactType>;
export declare namespace ArtifactType {
    type Raw = "recording" | "browser_console_log" | "skyvern_log" | "skyvern_log_raw" | "screenshot" | "screenshot_llm" | "screenshot_action" | "screenshot_final" | "llm_prompt" | "llm_request" | "llm_response" | "llm_response_parsed" | "llm_response_rendered" | "visible_elements_id_css_map" | "visible_elements_id_frame_map" | "visible_elements_tree" | "visible_elements_tree_trimmed" | "visible_elements_tree_in_prompt" | "hashed_href_map" | "visible_elements_id_xpath_map" | "html" | "html_scrape" | "html_action" | "trace" | "har" | "script_file";
}
