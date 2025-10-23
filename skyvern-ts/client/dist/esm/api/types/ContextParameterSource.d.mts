import type * as Skyvern from "../index.mjs";
export type ContextParameterSource = Skyvern.ContextParameterSource.Workflow | Skyvern.ContextParameterSource.Context | Skyvern.ContextParameterSource.AwsSecret | Skyvern.ContextParameterSource.AzureSecret | Skyvern.ContextParameterSource.BitwardenLoginCredential | Skyvern.ContextParameterSource.BitwardenSensitiveInformation | Skyvern.ContextParameterSource.BitwardenCreditCardData | Skyvern.ContextParameterSource.Onepassword | Skyvern.ContextParameterSource.AzureVaultCredential | Skyvern.ContextParameterSource.Output | Skyvern.ContextParameterSource.Credential;
export declare namespace ContextParameterSource {
    interface Workflow extends Skyvern.WorkflowParameter {
        parameter_type: "workflow";
    }
    interface Context extends Skyvern.ContextParameter {
        parameter_type: "context";
    }
    interface AwsSecret extends Skyvern.AwsSecretParameter {
        parameter_type: "aws_secret";
    }
    interface AzureSecret extends Skyvern.AzureSecretParameter {
        parameter_type: "azure_secret";
    }
    interface BitwardenLoginCredential extends Skyvern.BitwardenLoginCredentialParameter {
        parameter_type: "bitwarden_login_credential";
    }
    interface BitwardenSensitiveInformation extends Skyvern.BitwardenSensitiveInformationParameter {
        parameter_type: "bitwarden_sensitive_information";
    }
    interface BitwardenCreditCardData extends Skyvern.BitwardenCreditCardDataParameter {
        parameter_type: "bitwarden_credit_card_data";
    }
    interface Onepassword extends Skyvern.OnePasswordCredentialParameter {
        parameter_type: "onepassword";
    }
    interface AzureVaultCredential extends Skyvern.AzureVaultCredentialParameter {
        parameter_type: "azure_vault_credential";
    }
    interface Output extends Skyvern.OutputParameter {
        parameter_type: "output";
    }
    interface Credential extends Skyvern.CredentialParameter {
        parameter_type: "credential";
    }
}
