import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
export declare const BitwardenCreditCardDataParameter: core.serialization.ObjectSchema<serializers.BitwardenCreditCardDataParameter.Raw, Skyvern.BitwardenCreditCardDataParameter>;
export declare namespace BitwardenCreditCardDataParameter {
    interface Raw {
        key: string;
        description?: string | null;
        bitwarden_credit_card_data_parameter_id: string;
        workflow_id: string;
        bitwarden_client_id_aws_secret_key: string;
        bitwarden_client_secret_aws_secret_key: string;
        bitwarden_master_password_aws_secret_key: string;
        bitwarden_collection_id: string;
        bitwarden_item_id: string;
        created_at: string;
        modified_at: string;
        deleted_at?: string | null;
    }
}
