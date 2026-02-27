import { useEffect, useRef, useState } from "react";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { newWssBaseUrl, getRuntimeApiKey } from "@/util/env";

type StreamMessage = {
  browser_session_id?: string;
  status: string;
  screenshot?: string;
  format?: string;
};

interface Props {
  browserSessionId: string;
}

function BrowserSessionStream({ browserSessionId }: Props) {
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const [streamFormat, setStreamFormat] = useState<string>("png");
  const credentialGetter = useCredentialGetter();
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    async function run() {
      let credentialParam: string;
      if (credentialGetter) {
        const token = await credentialGetter();
        credentialParam = `token=Bearer ${token}`;
      } else {
        const apiKey = getRuntimeApiKey();
        credentialParam = apiKey ? `apikey=${apiKey}` : "";
      }

      if (socketRef.current) {
        socketRef.current.close();
      }
      socketRef.current = new WebSocket(
        `${newWssBaseUrl}/stream/browser_sessions/${browserSessionId}?${credentialParam}`,
      );

      socketRef.current.addEventListener("message", (event) => {
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (message.screenshot) {
            setStreamImgSrc(message.screenshot);
          }
          if (message.format) {
            setStreamFormat(message.format);
          }
          if (
            message.status === "completed" ||
            message.status === "failed" ||
            message.status === "timeout"
          ) {
            socketRef.current?.close();
          }
        } catch (e) {
          console.error("Failed to parse message", e);
        }
      });

      socketRef.current.addEventListener("close", () => {
        socketRef.current = null;
      });
    }
    run();

    return () => {
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [credentialGetter, browserSessionId]);

  if (streamImgSrc.length > 0) {
    return (
      <div className="h-full w-full">
        <ZoomableImage
          src={`data:image/${streamFormat};base64,${streamImgSrc}`}
          className="rounded-md"
        />
      </div>
    );
  }

  return (
    <div className="flex h-full w-full items-center justify-center text-sm text-slate-400">
      Starting stream...
    </div>
  );
}

export { BrowserSessionStream };
