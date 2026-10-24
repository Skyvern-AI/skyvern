import { isAxiosError } from "axios";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  getClient,
  removeAuthorizationHeader,
  type CredentialGetter,
} from "@/api/AxiosClient";
import { whenRuntimeCredentialReady } from "@/util/env";
import {
  createOidcUserManager,
  isOidcUserExpired,
  loadOidcRuntimeConfig,
  type OidcUser,
  type OidcUserManager,
} from "@/auth/oidc";
import { Button } from "@/components/ui/button";
import { CredentialGetterContext } from "@/store/CredentialGetterContext";
import {
  OidcAuthContext,
  type OidcAuthContextValue,
} from "@/store/OnPremOidcContext";
import {
  PageSlotsProvider,
  type PermissionsResponse,
  type PermissionsState,
} from "@/store/PageSlots";

const OIDC_CALLBACK_PATH = "/oidc/callback";

type AuthState =
  | Readonly<{ status: "loading" | "disabled" | "error" }>
  | Readonly<{
      status: "ready";
      manager: OidcUserManager;
      user: OidcUser | null;
    }>;

type CapabilitiesResponse = PermissionsResponse &
  Readonly<{
    rbac: Readonly<{
      enabled: boolean;
      enforce: boolean;
      effective_role: string | null;
    }>;
  }>;

function OidcPermissionsProvider({
  credentialGetter,
  children,
}: {
  credentialGetter: CredentialGetter;
  children: ReactNode;
}) {
  const [permissions, setPermissions] = useState<PermissionsState | undefined>({
    status: "loading",
  });

  useEffect(() => {
    let active = true;
    let retryTimeout: number | undefined;
    const loadCapabilities = async () => {
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const { data } =
          await client.get<CapabilitiesResponse>("/auth/capabilities");
        if (active) setPermissions({ status: "ready", data });
      } catch (error) {
        if (!active) return;
        if (
          isAxiosError(error) &&
          (error.response?.status === 401 || error.response?.status === 403)
        ) {
          setPermissions(undefined);
        } else {
          setPermissions({ status: "error" });
          const retryAfter = isAxiosError(error)
            ? Number(error.response?.headers["retry-after"])
            : Number.NaN;
          const retryDelay =
            Number.isFinite(retryAfter) && retryAfter > 0
              ? retryAfter * 1000
              : 2_000;
          retryTimeout = window.setTimeout(
            () => void loadCapabilities(),
            retryDelay,
          );
        }
      }
    };
    void loadCapabilities();
    return () => {
      active = false;
      if (retryTimeout !== undefined) window.clearTimeout(retryTimeout);
    };
  }, [credentialGetter]);

  return (
    <PageSlotsProvider value={{ permissions }}>{children}</PageSlotsProvider>
  );
}

function SignInScreen({ onSignIn }: { onSignIn: () => Promise<void> }) {
  const [failed, setFailed] = useState(false);

  return (
    <main className="flex min-h-screen items-center justify-center bg-background text-foreground">
      <section className="w-full max-w-sm space-y-4 rounded-lg border border-neutral-200 p-8 dark:border-white/[0.12]">
        <h1 className="text-xl font-semibold">Sign in to Skyvern</h1>
        <p className="text-sm text-muted-foreground">
          Continue with your identity provider.
        </p>
        {failed && (
          <p role="alert" className="text-sm text-destructive">
            Sign-in could not be started. Please try again.
          </p>
        )}
        <Button
          onClick={() => {
            void onSignIn().catch(() => setFailed(true));
          }}
        >
          Sign in
        </Button>
      </section>
    </main>
  );
}

function OnPremOidcProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const config = await loadOidcRuntimeConfig();
        if (!config.issuerUrl) {
          if (active) setState({ status: "disabled" });
          return;
        }
        if (!config.clientId) {
          if (active) setState({ status: "error" });
          return;
        }

        const manager = createOidcUserManager(config);
        let user = await manager.getUser();
        if (user && isOidcUserExpired(user)) {
          try {
            user = user.refresh_token ? await manager.signinSilent() : null;
          } catch {
            user = null;
          }
          if (!user) await manager.removeUser();
        }
        if (active) setState({ status: "ready", manager, user });
      } catch {
        if (active) setState({ status: "error" });
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  const updateUser = useCallback((user: OidcUser | null) => {
    setState((current) =>
      current.status === "ready" ? { ...current, user } : current,
    );
  }, []);

  useEffect(() => {
    if (state.status !== "ready" || !state.user) return;
    const { manager, user } = state;
    const expiry = user.expires_at * 1000;
    const renewBeforeExpiry = user.refresh_token ? 30_000 : 0;
    const timeout = window.setTimeout(
      () => {
        void (async () => {
          try {
            if (!user.refresh_token)
              throw new Error("Refresh token unavailable");
            const refreshed = await manager.signinSilent();
            if (!refreshed) throw new Error("Token refresh returned no user");
            updateUser(refreshed);
          } catch {
            await manager.removeUser().catch(() => undefined);
            removeAuthorizationHeader();
            updateUser(null);
          }
        })();
      },
      Math.max(0, expiry - Date.now() - renewBeforeExpiry),
    );
    return () => window.clearTimeout(timeout);
  }, [state, updateUser]);

  const manager = state.status === "ready" ? state.manager : null;
  const credentialGetter = useCallback(async () => {
    await whenRuntimeCredentialReady();
    const user = await manager?.getUser();
    return user && !isOidcUserExpired(user) ? user.access_token : null;
  }, [manager]);

  const signIn = useCallback(async () => {
    if (manager) {
      await manager.signinRedirect({
        state: {
          returnTo: `${window.location.pathname}${window.location.search}${window.location.hash}`,
        },
      });
    }
  }, [manager]);

  const signOut = useCallback(async () => {
    if (!manager) return;
    const user = state.status === "ready" ? state.user : null;
    await manager.removeUser().catch(() => undefined);
    removeAuthorizationHeader();
    updateUser(null);
    try {
      if (await manager.metadataService.getEndSessionEndpoint()) {
        await manager.signoutRedirect({ id_token_hint: user?.id_token });
      }
    } catch {
      // Local sign-out remains complete when the provider endpoint is unavailable.
    }
  }, [manager, state, updateUser]);

  const contextValue = useMemo<OidcAuthContextValue>(
    () => ({
      enabled: state.status === "ready",
      manager,
      user: state.status === "ready" ? state.user : null,
      signIn,
      signOut,
      completeSignIn: updateUser,
    }),
    [manager, signIn, signOut, state, updateUser],
  );

  return (
    <OidcAuthContext.Provider value={contextValue}>
      {state.status === "loading" ? (
        <main role="status" className="p-8 text-sm text-muted-foreground">
          Loading sign-in configuration…
        </main>
      ) : state.status === "error" ? (
        <main role="alert" className="p-8 text-sm text-destructive">
          Sign-in configuration is unavailable.
        </main>
      ) : state.status !== "ready" ? (
        children
      ) : (
        <CredentialGetterContext.Provider value={credentialGetter}>
          {state.user ? (
            <OidcPermissionsProvider credentialGetter={credentialGetter}>
              {children}
            </OidcPermissionsProvider>
          ) : window.location.pathname === OIDC_CALLBACK_PATH ? (
            children
          ) : (
            <SignInScreen onSignIn={signIn} />
          )}
        </CredentialGetterContext.Provider>
      )}
    </OidcAuthContext.Provider>
  );
}

export { OnPremOidcProvider };
