import {
  createContext,
  createElement,
  useContext,
  type ReactNode,
} from "react";

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
  CredentialAuthenticatorSupportCopy,
  CredentialAuthenticatorQrCodeType,
  CredentialEnterpriseAuthenticatorSupport,
};
