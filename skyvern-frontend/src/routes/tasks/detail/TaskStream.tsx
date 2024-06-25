import { Status } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

type StreamMessage = {
  task_id: string;
  status: string;
  screenshot?: string;
};

let socket: WebSocket | null = null;

type Props = {
  status: Status;
};

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function TaskStream({ status }: Props) {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();
  const [imgSrc, setImgSrc] = useState<string>("");

  useEffect(() => {
    if (!taskId || !credentialGetter) {
      console.error("TaskStream: Task ID is required");
      return;
    }

    async function run() {
      // Create WebSocket connection.
      const credential = await credentialGetter!();
      if (socket) {
        socket.close();
      }
      socket = new WebSocket(
        `${wssBaseUrl}/stream/tasks/${taskId}?token=Bearer ${credential}`,
      );

      socket.addEventListener("open", (event) => {
        console.log("open event", event);
      });

      // Listen for messages
      socket.addEventListener("message", (event) => {
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (message.screenshot) {
            setImgSrc(message.screenshot);
          }
          if (message.status === "completed") {
            socket?.close();
          }
        } catch (e) {
          console.error("Failed to parse message", e);
        }
      });

      socket.addEventListener("close", (event) => {
        console.log("close event", event);
        socket = null;
      });
    }

    run();

    return () => {
      if (socket) {
        socket.close();
        socket = null;
      }
    };
  }, [credentialGetter, taskId]);

  if (status === Status.Queued) {
    return (
      <div className="w-full h-full flex flex-col gap-4 items-center justify-center text-lg bg-slate-900">
        <span>Your task is queued. Typical queue time is 1-2 minutes.</span>
        <span>Stream will start when the task is running.</span>
      </div>
    );
  }

  if (status === Status.Running && imgSrc.length === 0) {
    return (
      <div className="w-full h-full flex items-center justify-center text-lg bg-slate-900">
        Starting the stream...
      </div>
    );
  }

  if (status === Status.Running && imgSrc.length > 0) {
    return (
      <div className="w-full h-full">
        <img src={`data:image/png;base64,${imgSrc}`} />
      </div>
    );
  }

  return null;
}

export { TaskStream };
