import { useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useGoogleOAuthCredentials } from "@/hooks/useGoogleOAuthCredentials";
import { useToast } from "@/components/ui/use-toast";
import { clearStoredGoogleOAuthIntegrationIdForState } from "./googleOAuth";

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

    const finish = async () => {
      const error = searchParams.get("error");
      const code = searchParams.get("code");
      const state = searchParams.get("state");

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

      try {
        await submitOAuthCallbackAsync({ code, state });
        queryClient.invalidateQueries({ queryKey: ["googleOAuthCredentials"] });
      } finally {
        clearStoredGoogleOAuthIntegrationIdForState(state);
        navigate("/integrations", { replace: true });
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
