import type * as Skyvern from "../index.js";
export type FileDownloadBlockParametersItem = Skyvern.FileDownloadBlockParametersItem.AwsSecret | Skyvern.FileDownloadBlockParametersItem.AzureSecret | Skyvern.FileDownloadBlockParametersItem.AzureVaultCredential | Skyvern.FileDownloadBlockParametersItem.BitwardenCreditCardData | Skyvern.FileDownloadBlockParametersItem.BitwardenLoginCredential | Skyvern.FileDownloadBlockParametersItem.BitwardenSensitiveInformation | Skyvern.FileDownloadBlockParametersItem.Context | Skyvern.FileDownloadBlockParametersItem.Credential | Skyvern.FileDownloadBlockParametersItem.Onepassword | Skyvern.FileDownloadBlockParametersItem.Output | Skyvern.FileDownloadBlockParametersItem.Workflow;
export declare namespace FileDownloadBlockParametersItem {
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
