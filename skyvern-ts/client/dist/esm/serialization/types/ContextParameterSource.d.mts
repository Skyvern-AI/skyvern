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
export declare const ContextParameterSource: core.serialization.Schema<serializers.ContextParameterSource.Raw, Skyvern.ContextParameterSource>;
export declare namespace ContextParameterSource {
    type Raw = ContextParameterSource.Workflow | ContextParameterSource.Context | ContextParameterSource.AwsSecret | ContextParameterSource.AzureSecret | ContextParameterSource.BitwardenLoginCredential | ContextParameterSource.BitwardenSensitiveInformation | ContextParameterSource.BitwardenCreditCardData | ContextParameterSource.Onepassword | ContextParameterSource.AzureVaultCredential | ContextParameterSource.Output | ContextParameterSource.Credential;
    interface Workflow extends WorkflowParameter.Raw {
        parameter_type: "workflow";
    }
    interface Context extends serializers.ContextParameter.Raw {
        parameter_type: "context";
    }
    interface AwsSecret extends AwsSecretParameter.Raw {
        parameter_type: "aws_secret";
    }
    interface AzureSecret extends AzureSecretParameter.Raw {
        parameter_type: "azure_secret";
    }
    interface BitwardenLoginCredential extends BitwardenLoginCredentialParameter.Raw {
        parameter_type: "bitwarden_login_credential";
    }
    interface BitwardenSensitiveInformation extends BitwardenSensitiveInformationParameter.Raw {
        parameter_type: "bitwarden_sensitive_information";
    }
    interface BitwardenCreditCardData extends BitwardenCreditCardDataParameter.Raw {
        parameter_type: "bitwarden_credit_card_data";
    }
    interface Onepassword extends OnePasswordCredentialParameter.Raw {
        parameter_type: "onepassword";
    }
    interface AzureVaultCredential extends AzureVaultCredentialParameter.Raw {
        parameter_type: "azure_vault_credential";
    }
    interface Output extends OutputParameter.Raw {
        parameter_type: "output";
    }
    interface Credential extends CredentialParameter.Raw {
        parameter_type: "credential";
    }
}
