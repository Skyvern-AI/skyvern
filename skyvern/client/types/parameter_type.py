# This file defines the ParameterType for client-side type checking.
import typing

ParameterType = typing.Union[
    typing.Literal["aws_secret"],
    typing.Literal["bitwarden_login_credential"],
    typing.Literal["bitwarden_credit_card_data"],
    typing.Literal["bitwarden_sensitive_information"],
    typing.Literal["credential"], # General credential parameter
    typing.Literal["onepassword_login_credential"], # New type
    typing.Literal["workflow"], # For workflow parameters
    typing.Literal["output"], # For output parameters
    typing.Literal["context"], # For context parameters
    typing.Any, # As a fallback, similar to CredentialType
]
