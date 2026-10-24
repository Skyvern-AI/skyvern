import { useContext, useEffect } from "react";
import { useNavigate } from "react-router-dom";

import { removeAuthorizationHeader } from "@/api/AxiosClient";
import type { OidcUser, OidcUserManager } from "@/auth/oidc";
import { OidcAuthContext } from "@/store/OnPremOidcContext";

let callbackRequest: { url: string; promise: Promise<OidcUser> } | undefined;

function signinRedirectCallbackOnce(manager: OidcUserManager) {
  const callbackUrl = window.location.href;
  if (callbackRequest?.url === callbackUrl) return callbackRequest.promise;

  const request = Promise.resolve()
    .then(() => manager.signinRedirectCallback())
    .catch(async (error: unknown) => {
      await manager.removeUser().catch(() => undefined);
      removeAuthorizationHeader();
      throw error;
    });
  callbackRequest = { url: callbackUrl, promise: request };
  return request;
}

function getReturnTo(state: unknown): string {
  if (
    typeof state !== "object" ||
    state === null ||
    Array.isArray(state) ||
    !("returnTo" in state) ||
    typeof state.returnTo !== "string"
  ) {
    return "/";
  }

  const returnTo = state.returnTo;
  if (
    !returnTo.startsWith("/") ||
    returnTo.startsWith("//") ||
    returnTo.includes("\\")
  ) {
    return "/";
  }

  try {
    const url = new URL(returnTo, window.location.origin);
    if (url.origin !== window.location.origin) return "/";
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "/";
  }
}

function OidcCallback() {
  const { enabled, manager, completeSignIn } = useContext(OidcAuthContext);
  const navigate = useNavigate();

  useEffect(() => {
    if (!enabled || !manager) {
      navigate("/", { replace: true });
      return;
    }

    let active = true;
    void signinRedirectCallbackOnce(manager)
      .then((user) => {
        if (active) {
          completeSignIn(user);
          navigate(getReturnTo(user.state), { replace: true });
        }
      })
      .catch(() => {
        if (active) navigate("/", { replace: true });
      });

    return () => {
      active = false;
    };
  }, [completeSignIn, enabled, manager, navigate]);

  return (
    <main role="status" className="p-8 text-sm text-muted-foreground">
      Completing sign-in…
    </main>
  );
}

export { OidcCallback };
