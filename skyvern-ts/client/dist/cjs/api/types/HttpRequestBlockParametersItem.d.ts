import type * as Skyvern from "../index.js";
export type HttpRequestBlockParametersItem = Skyvern.HttpRequestBlockParametersItem.AwsSecret | Skyvern.HttpRequestBlockParametersItem.AzureSecret | Skyvern.HttpRequestBlockParametersItem.AzureVaultCredential | Skyvern.HttpRequestBlockParametersItem.BitwardenCreditCardData | Skyvern.HttpRequestBlockParametersItem.BitwardenLoginCredential | Skyvern.HttpRequestBlockParametersItem.BitwardenSensitiveInformation | Skyvern.HttpRequestBlockParametersItem.Context | Skyvern.HttpRequestBlockParametersItem.Credential | Skyvern.HttpRequestBlockParametersItem.Onepassword | Skyvern.HttpRequestBlockParametersItem.Output | Skyvern.HttpRequestBlockParametersItem.Workflow;
export declare namespace HttpRequestBlockParametersItem {
    interface AwsSecret extends Skyvern.AwsSecretParameter {
        parameter_type: "aws_secret";
    }
    interface AzureSecret extends Skyvern.AzureSecretParameter {
        parameter_type: "azure_secret";
    }
    interface AzureVaultCredential extends Skyvern.AzureVaultCredentialParameter {
        parameter_type: "azure_vault_credential";
    }
    interface BitwardenCreditCardData extends Skyvern.BitwardenCreditCardDataParameter {
        parameter_type: "bitwarden_credit_card_data";
    }
    interface BitwardenLoginCredential extends Skyvern.BitwardenLoginCredentialParameter {
        parameter_type: "bitwarden_login_credential";
    }
    interface BitwardenSensitiveInformation extends Skyvern.BitwardenSensitiveInformationParameter {
        parameter_type: "bitwarden_sensitive_information";
    }
    interface Context extends Skyvern.ContextParameter {
        parameter_type: "context";
    }
    interface Credential extends Skyvern.CredentialParameter {
        parameter_type: "credential";
    }
    interface Onepassword extends Skyvern.OnePasswordCredentialParameter {
        parameter_type: "onepassword";
    }
    interface Output extends Skyvern.OutputParameter {
        parameter_type: "output";
    }
    interface Workflow extends Skyvern.WorkflowParameter {
        parameter_type: "workflow";
    }
}
