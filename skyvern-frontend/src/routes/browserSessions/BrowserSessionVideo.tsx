import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

interface Recording {
  url: string;
  checksum: string;
  filename: string;
  modified_at: string;
}

function BrowserSessionVideo() {
  const { browserSessionId } = useParams();
  const credentialGetter = useCredentialGetter();

  const {
    data: browserSession,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["browserSession", browserSessionId],
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get(
        `/browser_sessions/${browserSessionId}`,
      );
      return response.data;
    },
    enabled: !!browserSessionId,
  });

  const recordings = browserSession?.recordings || [];

  if (isLoading) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="text-lg">Loading videos...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="text-lg text-red-500">
          Error loading videos: {error.message}
        </div>
      </div>
    );
  }

  if (!recordings || recordings.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="text-center">
          <div className="mb-2 text-lg text-gray-500">
            No recordings available
          </div>
          <div className="text-sm text-gray-400">
            Video recordings will appear here when the browser session is active
            and recording
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full w-full p-4">
      <div className="mb-4">
        <h2 className="text-xl font-semibold">Browser Session Videos</h2>
        <p className="text-sm text-gray-500">
          Recorded videos from this browser session
        </p>
      </div>

      <div className="grid gap-4">
        {recordings.map((recording: Recording, index: number) => (
          <div
            key={recording.checksum || index}
            className="rounded-lg border p-4"
          >
            <div className="mb-2">
              <h3 className="font-medium">
                {recording.filename || `Recording ${index + 1}`}
                {recording.modified_at && (
                  <span className="ml-2 text-sm text-gray-500">
                    ({new Date(recording.modified_at).toLocaleString()})
                  </span>
                )}
              </h3>
            </div>

            {recording.url ? (
              <div className="w-full">
                <video
                  controls
                  className="w-full max-w-4xl rounded-lg"
                  src={recording.url}
                  preload="metadata"
                >
                  Your browser does not support the video tag.
                </video>
                <div className="mt-2 text-xs text-gray-500">
                  <a
                    href={recording.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-600 hover:text-blue-800"
                  >
                    Download video
                  </a>
                </div>
              </div>
            ) : (
              <div className="text-gray-500">
                Video URL not available - video may still be processing
              </div>
            )}

            {recording.checksum && (
              <div className="mt-2 text-sm text-gray-600">
                Checksum: {recording.checksum}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

export { BrowserSessionVideo };
