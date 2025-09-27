import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { BrowserStream } from "@/components/BrowserStream";
import { LogoMinimized } from "@/components/LogoMinimized";
import { SwitchBar } from "@/components/SwitchBar";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { BrowserSessionVideo } from "./BrowserSessionVideo";
import { cn } from "@/util/utils";

type TabName = "stream" | "videos";

function BrowserSession() {
  const { browserSessionId } = useParams();
  const [hasBrowserSession, setHasBrowserSession] = useState(false);
  const [activeTab, setActiveTab] = useState<TabName>("stream");

  const credentialGetter = useCredentialGetter();

  const query = useQuery({
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");

      try {
        const response = await client.get(
          `/browser_sessions/${browserSessionId}`,
        );
        setHasBrowserSession(true);
        return response.data;
      } catch (error) {
        setHasBrowserSession(false);
        return null;
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

        {/* Tab Navigation */}
        <SwitchBar
          className="mb-2 border-none"
          onChange={(value) => setActiveTab(value as TabName)}
          value={activeTab}
          options={[
            {
              label: "Stream",
              value: "stream",
              helpText: "The live stream of the browser session (if active).",
            },
            {
              label: "Recordings",
              value: "videos",
              helpText: "All recordings of this browser session.",
            },
          ]}
        />

        {/* Tab Content */}
        <div className="relative min-h-0 w-full flex-1 rounded-lg border p-4">
          <div
            className={cn(
              "absolute left-0 top-0 z-10 flex h-full w-full items-center justify-center",
              {
                hidden: activeTab !== "stream",
              },
            )}
          >
            <BrowserStream
              browserSessionId={browserSessionId}
              interactive={false}
              showControlButtons={true}
            />
          </div>
          {activeTab === "videos" && <BrowserSessionVideo />}
        </div>
      </div>
    </div>
  );
}

export { BrowserSession };
