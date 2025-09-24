import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { BrowserStream } from "@/components/BrowserStream";
import { LogoMinimized } from "@/components/LogoMinimized";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { BrowserSessionVideo } from "./BrowserSessionVideo";

function BrowserSession() {
  const { browserSessionId } = useParams();
  const [hasBrowserSession, setHasBrowserSession] = useState(false);
  const [activeTab, setActiveTab] = useState<"stream" | "videos">("stream");

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
        <div className="flex w-full border-b">
          <button
            className={`border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === "stream"
                ? "border-blue-500 text-blue-600"
                : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
            onClick={() => setActiveTab("stream")}
          >
            Live Stream
          </button>
          <button
            className={`border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
              activeTab === "videos"
                ? "border-blue-500 text-blue-600"
                : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
            onClick={() => setActiveTab("videos")}
          >
            Recordings
          </button>
        </div>

        {/* Tab Content */}
        <div className="min-h-0 w-full flex-1 rounded-lg border p-4">
          {activeTab === "stream" && (
            <BrowserStream
              browserSessionId={browserSessionId}
              interactive={false}
              showControlButtons={true}
            />
          )}
          {activeTab === "videos" && <BrowserSessionVideo />}
        </div>
      </div>
    </div>
  );
}

export { BrowserSession };
