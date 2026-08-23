import {
  createContext,
  createElement,
  useContext,
  type ReactNode,
} from "react";
import type { CredentialGetter } from "@/api/AxiosClient";
import type { PasswordCredential } from "@/api/types";

type CredentialAdditionalTwoFactorState = Record<
  string,
  string | number | boolean
>;

type CredentialAdditionalTwoFactorMethod = {
  value: string;
  requestType: PasswordCredential["totp_type"];
  label: string;
  icon?: ReactNode;
  flagName?: string;
  supportsInlineTest?: boolean;
  removalConfirmation?: string;
  initialState?: CredentialAdditionalTwoFactorState;
  renderFields: (props: {
    state: CredentialAdditionalTwoFactorState;
    setState: (next: CredentialAdditionalTwoFactorState) => void;
    disabled: boolean;
    isEditMode: boolean;
    configured: boolean;
    validationErrorId?: string;
  }) => ReactNode;
  validate: (
    state: CredentialAdditionalTwoFactorState,
    context: { isEditMode: boolean; configured: boolean; enabled: boolean },
  ) => string | null;
  onSaved: (args: {
    credentialId: string;
    state: CredentialAdditionalTwoFactorState;
    wasSelected: boolean;
    previouslyConfigured: boolean;
    enabled: boolean;
    credentialGetter: CredentialGetter | null;
  }) => Promise<void>;
};

type CredentialEnterpriseAuthenticatorSupport = {
  label: string;
  apps: string[];
  description: ReactNode;
  contactUrl?: string;
  vendorLabels?: Record<string, string>;
  qrCodeTypes?: CredentialAuthenticatorQrCodeType[];
  inferQrCodeType?: (value: string) => string | null;
};

type CredentialAuthenticatorQrCodeType = {
  id: string;
  label: string;
  description?: ReactNode;
  logo?: ReactNode;
};

type CredentialAuthenticatorSupportCopy = {
  enterpriseApps?: CredentialEnterpriseAuthenticatorSupport;
  additionalTwoFactorMethods?: CredentialAdditionalTwoFactorMethod[];
};

const CredentialAuthenticatorSupportContext =
  createContext<CredentialAuthenticatorSupportCopy>({});

function CredentialAuthenticatorSupportProvider({
  children,
  value,
}: {
  children: ReactNode;
  value: CredentialAuthenticatorSupportCopy;
}) {
  return createElement(
    CredentialAuthenticatorSupportContext.Provider,
    { value },
    children,
  );
}

function useCredentialAuthenticatorSupport() {
  return useContext(CredentialAuthenticatorSupportContext);
}

export {
  CredentialAuthenticatorSupportProvider,
  useCredentialAuthenticatorSupport,
};
export type {
  CredentialAdditionalTwoFactorMethod,
  CredentialAdditionalTwoFactorState,
  CredentialAuthenticatorSupportCopy,
  CredentialAuthenticatorQrCodeType,
  CredentialEnterpriseAuthenticatorSupport,
};
