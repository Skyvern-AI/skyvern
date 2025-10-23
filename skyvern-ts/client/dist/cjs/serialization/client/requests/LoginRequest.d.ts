import type * as Skyvern from "../../../api/index.js";
import * as core from "../../../core/index.js";
import type * as serializers from "../../index.js";
import { ProxyLocation } from "../../types/ProxyLocation.js";
import { SkyvernSchemasRunBlocksCredentialType } from "../../types/SkyvernSchemasRunBlocksCredentialType.js";
export declare const LoginRequest: core.serialization.Schema<serializers.LoginRequest.Raw, Skyvern.LoginRequest>;
export declare namespace LoginRequest {
    interface Raw {
        credential_type: SkyvernSchemasRunBlocksCredentialType.Raw;
        url?: string | null;
        prompt?: string | null;
        webhook_url?: string | null;
        proxy_location?: ProxyLocation.Raw | null;
        totp_identifier?: string | null;
        totp_url?: string | null;
        browser_session_id?: string | null;
        browser_address?: string | null;
        extra_http_headers?: Record<string, string | null | undefined> | null;
        max_screenshot_scrolling_times?: number | null;
        credential_id?: string | null;
        bitwarden_collection_id?: string | null;
        bitwarden_item_id?: string | null;
        onepassword_vault_id?: string | null;
        onepassword_item_id?: string | null;
        azure_vault_name?: string | null;
        azure_vault_username_key?: string | null;
        azure_vault_password_key?: string | null;
        azure_vault_totp_secret_key?: string | null;
    }
}
