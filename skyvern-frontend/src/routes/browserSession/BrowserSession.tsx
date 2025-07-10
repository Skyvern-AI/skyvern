import { useState } from "react";
import { useParams } from "react-router-dom";
import { BrowserStream } from "@/components/BrowserStream";
import { getClient } from "@/api/AxiosClient";

import { useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

function BrowserSession() {
  const { browserSessionId } = useParams();
  const [hasBrowserSession, setHasBrowserSession] = useState(false);

  const credentialGetter = useCredentialGetter();

  const query = useQuery({
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");

      try {
        await client.get(`/browser_sessions/${browserSessionId}`);
        setHasBrowserSession(true);
        return true;
      } catch (error) {
        setHasBrowserSession(false);
        return false;
      }
    },
  });

  if (query.isLoading) {
    return (
      <div className="h-screen w-full gap-4 p-6">
        <div className="flex h-full w-full items-center justify-center">
          {/* we need nice artwork here */}
          Loading...
        </div>
      </div>
    );
  }

  if (!hasBrowserSession) {
    return (
      <div className="h-screen w-full gap-4 p-6">
        <div className="flex h-full w-full items-center justify-center">
          {/* we need nice artwork here */}
          No browser session found.
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen w-full gap-4 p-6">
      <div className="flex h-full w-full items-center justify-center">
        <BrowserStream browserSessionId={browserSessionId} />
      </div>
    </div>
  );
}

export { BrowserSession };
