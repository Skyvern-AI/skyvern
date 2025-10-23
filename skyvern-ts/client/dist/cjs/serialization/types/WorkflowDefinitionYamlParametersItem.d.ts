import type * as Skyvern from "../../api/index.js";
import * as core from "../../core/index.js";
import type * as serializers from "../index.js";
import { AwsSecretParameterYaml } from "./AwsSecretParameterYaml.js";
import { AzureVaultCredentialParameterYaml } from "./AzureVaultCredentialParameterYaml.js";
import { BitwardenCreditCardDataParameterYaml } from "./BitwardenCreditCardDataParameterYaml.js";
import { BitwardenLoginCredentialParameterYaml } from "./BitwardenLoginCredentialParameterYaml.js";
import { BitwardenSensitiveInformationParameterYaml } from "./BitwardenSensitiveInformationParameterYaml.js";
import { ContextParameterYaml } from "./ContextParameterYaml.js";
import { CredentialParameterYaml } from "./CredentialParameterYaml.js";
import { OnePasswordCredentialParameterYaml } from "./OnePasswordCredentialParameterYaml.js";
import { OutputParameterYaml } from "./OutputParameterYaml.js";
import { WorkflowParameterYaml } from "./WorkflowParameterYaml.js";
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
