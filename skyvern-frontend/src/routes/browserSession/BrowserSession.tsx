import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { BrowserStream } from "@/components/BrowserStream";
import { LogoMinimized } from "@/components/LogoMinimized";
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
      <div className="flex h-full w-full flex-col items-start justify-start gap-2">
        <div className="flex w-full flex-shrink-0 flex-row items-center justify-between rounded-lg border p-4">
          <div className="flex flex-row items-center justify-start gap-2">
            <LogoMinimized />
            <div className="text-xl">browser session</div>
          </div>
        </div>
        <div className="min-h-0 w-full flex-1 rounded-lg border p-4">
          <BrowserStream
            browserSessionId={browserSessionId}
            interactive={false}
            showControlButtons={true}
          />
        </div>
      </div>
    </div>
  );
}

export { BrowserSession };
