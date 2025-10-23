import type * as Skyvern from "../../api/index.mjs";
import * as core from "../../core/index.mjs";
import type * as serializers from "../index.mjs";
import { AwsSecretParameterYaml } from "./AwsSecretParameterYaml.mjs";
import { AzureVaultCredentialParameterYaml } from "./AzureVaultCredentialParameterYaml.mjs";
import { BitwardenCreditCardDataParameterYaml } from "./BitwardenCreditCardDataParameterYaml.mjs";
import { BitwardenLoginCredentialParameterYaml } from "./BitwardenLoginCredentialParameterYaml.mjs";
import { BitwardenSensitiveInformationParameterYaml } from "./BitwardenSensitiveInformationParameterYaml.mjs";
import { ContextParameterYaml } from "./ContextParameterYaml.mjs";
import { CredentialParameterYaml } from "./CredentialParameterYaml.mjs";
import { OnePasswordCredentialParameterYaml } from "./OnePasswordCredentialParameterYaml.mjs";
import { OutputParameterYaml } from "./OutputParameterYaml.mjs";
import { WorkflowParameterYaml } from "./WorkflowParameterYaml.mjs";
export declare const WorkflowDefinitionYamlParametersItem: core.serialization.Schema<serializers.WorkflowDefinitionYamlParametersItem.Raw, Skyvern.WorkflowDefinitionYamlParametersItem>;
export declare namespace WorkflowDefinitionYamlParametersItem {
    type Raw = WorkflowDefinitionYamlParametersItem.AwsSecret | WorkflowDefinitionYamlParametersItem.AzureVaultCredential | WorkflowDefinitionYamlParametersItem.BitwardenCreditCardData | WorkflowDefinitionYamlParametersItem.BitwardenLoginCredential | WorkflowDefinitionYamlParametersItem.BitwardenSensitiveInformation | WorkflowDefinitionYamlParametersItem.Context | WorkflowDefinitionYamlParametersItem.Credential | WorkflowDefinitionYamlParametersItem.Onepassword | WorkflowDefinitionYamlParametersItem.Output | WorkflowDefinitionYamlParametersItem.Workflow;
    interface AwsSecret extends AwsSecretParameterYaml.Raw {
        parameter_type: "aws_secret";
    }
    interface AzureVaultCredential extends AzureVaultCredentialParameterYaml.Raw {
        parameter_type: "azure_vault_credential";
    }
    interface BitwardenCreditCardData extends BitwardenCreditCardDataParameterYaml.Raw {
        parameter_type: "bitwarden_credit_card_data";
    }
    interface BitwardenLoginCredential extends BitwardenLoginCredentialParameterYaml.Raw {
        parameter_type: "bitwarden_login_credential";
    }
    interface BitwardenSensitiveInformation extends BitwardenSensitiveInformationParameterYaml.Raw {
        parameter_type: "bitwarden_sensitive_information";
    }
    interface Context extends ContextParameterYaml.Raw {
        parameter_type: "context";
    }
    interface Credential extends CredentialParameterYaml.Raw {
        parameter_type: "credential";
    }
    interface Onepassword extends OnePasswordCredentialParameterYaml.Raw {
        parameter_type: "onepassword";
    }
    interface Output extends OutputParameterYaml.Raw {
        parameter_type: "output";
    }
    interface Workflow extends WorkflowParameterYaml.Raw {
        parameter_type: "workflow";
    }
}
