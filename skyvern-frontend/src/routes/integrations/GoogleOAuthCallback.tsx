import { useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  broadcastGoogleOAuthCredentialsChanged,
  useGoogleOAuthCredentials,
} from "@/hooks/useGoogleOAuthCredentials";
import { useToast } from "@/components/ui/use-toast";
import {
  clearStoredGoogleOAuthIntegrationIdForState,
  getStoredGoogleOAuthIntegrationIdForState,
} from "./googleOAuth";
import { closeGoogleOAuthPopupIfMarked } from "./googleOAuthPopup";

function GoogleOAuthCallback() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { submitOAuthCallbackAsync } = useGoogleOAuthCredentials();
  const { toast } = useToast();
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;

    const state = searchParams.get("state");
    const bouncedCredentialId = searchParams.get("credential_id");
    const bouncedSuccess = searchParams.get("success") === "1";
    const storedIntegrationId = state
      ? getStoredGoogleOAuthIntegrationIdForState(state)
      : null;

    // A hosted callback exchanges the code, then returns the popup to its
    // original app origin. Back on that origin, sessionStorage and
    // BroadcastChannel address the studio tab that opened the popup.
    if (bouncedCredentialId && bouncedSuccess && storedIntegrationId) {
      if (state) {
        clearStoredGoogleOAuthIntegrationIdForState(state);
      }
      queryClient.invalidateQueries({ queryKey: ["googleOAuthCredentials"] });
      broadcastGoogleOAuthCredentialsChanged();
      toast({
        title: "Success",
        description: "Google account connected successfully",
      });
      if (!closeGoogleOAuthPopupIfMarked()) {
        navigate("/integrations", { replace: true });
      }
      return;
    }

    const finish = async () => {
      const error = searchParams.get("error");
      const code = searchParams.get("code");

      if (error) {
        toast({
          title: "Google connection cancelled",
          description: error,
          variant: "destructive",
        });
        navigate("/integrations", { replace: true });
        return;
      }
      if (!code || !state) {
        toast({
          title: "Missing OAuth parameters",
          description: "The callback URL was missing a code or state value.",
          variant: "destructive",
        });
        navigate("/integrations", { replace: true });
        return;
      }

      let connected = false;
      try {
        await submitOAuthCallbackAsync({ code, state });
        queryClient.invalidateQueries({ queryKey: ["googleOAuthCredentials"] });
        connected = true;
      } finally {
        clearStoredGoogleOAuthIntegrationIdForState(state);
        if (!connected || !closeGoogleOAuthPopupIfMarked()) {
          navigate("/integrations", { replace: true });
        }
      }
    };

    void finish();
  }, [searchParams, navigate, queryClient, submitOAuthCallbackAsync, toast]);

  return (
    <div className="flex min-h-[40vh] items-center justify-center">
      <p className="text-sm text-muted-foreground">
        Finishing Google connection...
      </p>
    </div>
  );
}

export { GoogleOAuthCallback };
