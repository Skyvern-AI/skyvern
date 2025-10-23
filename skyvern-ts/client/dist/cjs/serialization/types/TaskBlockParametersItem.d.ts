import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import * as serializers from "../index.js";
import { AwsSecretParameter } from "./AwsSecretParameter.js";
import { AzureSecretParameter } from "./AzureSecretParameter.js";
import { AzureVaultCredentialParameter } from "./AzureVaultCredentialParameter.js";
import { BitwardenCreditCardDataParameter } from "./BitwardenCreditCardDataParameter.js";
import { BitwardenLoginCredentialParameter } from "./BitwardenLoginCredentialParameter.js";
import { BitwardenSensitiveInformationParameter } from "./BitwardenSensitiveInformationParameter.js";
import { CredentialParameter } from "./CredentialParameter.js";
import { OnePasswordCredentialParameter } from "./OnePasswordCredentialParameter.js";
import { OutputParameter } from "./OutputParameter.js";
import { WorkflowParameter } from "./WorkflowParameter.js";
export declare const TaskBlockParametersItem: core.serialization.Schema<serializers.TaskBlockParametersItem.Raw, Skyvern.TaskBlockParametersItem>;
export declare namespace TaskBlockParametersItem {
    type Raw = TaskBlockParametersItem.AwsSecret | TaskBlockParametersItem.AzureSecret | TaskBlockParametersItem.AzureVaultCredential | TaskBlockParametersItem.BitwardenCreditCardData | TaskBlockParametersItem.BitwardenLoginCredential | TaskBlockParametersItem.BitwardenSensitiveInformation | TaskBlockParametersItem.Context | TaskBlockParametersItem.Credential | TaskBlockParametersItem.Onepassword | TaskBlockParametersItem.Output | TaskBlockParametersItem.Workflow;
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
