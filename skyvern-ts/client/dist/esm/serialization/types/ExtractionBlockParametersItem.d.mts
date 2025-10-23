import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import * as serializers from "../index.mjs";
import { AwsSecretParameter } from "./AwsSecretParameter.mjs";
import { AzureSecretParameter } from "./AzureSecretParameter.mjs";
import { AzureVaultCredentialParameter } from "./AzureVaultCredentialParameter.mjs";
import { BitwardenCreditCardDataParameter } from "./BitwardenCreditCardDataParameter.mjs";
import { BitwardenLoginCredentialParameter } from "./BitwardenLoginCredentialParameter.mjs";
import { BitwardenSensitiveInformationParameter } from "./BitwardenSensitiveInformationParameter.mjs";
import { CredentialParameter } from "./CredentialParameter.mjs";
import { OnePasswordCredentialParameter } from "./OnePasswordCredentialParameter.mjs";
import { OutputParameter } from "./OutputParameter.mjs";
import { WorkflowParameter } from "./WorkflowParameter.mjs";
export declare const ExtractionBlockParametersItem: core.serialization.Schema<serializers.ExtractionBlockParametersItem.Raw, Skyvern.ExtractionBlockParametersItem>;
export declare namespace ExtractionBlockParametersItem {
    type Raw = ExtractionBlockParametersItem.AwsSecret | ExtractionBlockParametersItem.AzureSecret | ExtractionBlockParametersItem.AzureVaultCredential | ExtractionBlockParametersItem.BitwardenCreditCardData | ExtractionBlockParametersItem.BitwardenLoginCredential | ExtractionBlockParametersItem.BitwardenSensitiveInformation | ExtractionBlockParametersItem.Context | ExtractionBlockParametersItem.Credential | ExtractionBlockParametersItem.Onepassword | ExtractionBlockParametersItem.Output | ExtractionBlockParametersItem.Workflow;
    interface AwsSecret extends AwsSecretParameter.Raw {
        parameter_type: "aws_secret";
    }
    interface AzureSecret extends AzureSecretParameter.Raw {
        parameter_type: "azure_secret";
    }
    interface AzureVaultCredential extends AzureVaultCredentialParameter.Raw {
        parameter_type: "azure_vault_credential";
    }
    interface BitwardenCreditCardData extends BitwardenCreditCardDataParameter.Raw {
        parameter_type: "bitwarden_credit_card_data";
    }
    interface BitwardenLoginCredential extends BitwardenLoginCredentialParameter.Raw {
        parameter_type: "bitwarden_login_credential";
    }
    interface BitwardenSensitiveInformation extends BitwardenSensitiveInformationParameter.Raw {
        parameter_type: "bitwarden_sensitive_information";
    }
    interface Context extends serializers.ContextParameter.Raw {
        parameter_type: "context";
    }
    interface Credential extends CredentialParameter.Raw {
        parameter_type: "credential";
    }
    interface Onepassword extends OnePasswordCredentialParameter.Raw {
        parameter_type: "onepassword";
    }
    interface Output extends OutputParameter.Raw {
        parameter_type: "output";
    }
    interface Workflow extends WorkflowParameter.Raw {
        parameter_type: "workflow";
    }
}
