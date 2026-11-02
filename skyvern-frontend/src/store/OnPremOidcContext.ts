import { createContext } from "react";
import type { OidcUser, OidcUserManager } from "@/auth/oidc";

export type OidcAuthContextValue = Readonly<{
  enabled: boolean;
  manager: OidcUserManager | null;
  user: OidcUser | null;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
  completeSignIn: (user: OidcUser) => void;
}>;

export const OidcAuthContext = createContext<OidcAuthContextValue>({
  enabled: false,
  manager: null,
  user: null,
  signIn: async () => undefined,
  signOut: async () => undefined,
  completeSignIn: () => undefined,
});
