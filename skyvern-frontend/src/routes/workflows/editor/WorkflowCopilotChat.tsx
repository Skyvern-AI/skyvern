import { useState, useEffect, useRef, memo } from "react";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useIsSkyvernUser } from "@/hooks/useIsSkyvernUser";
import { useParams } from "react-router-dom";
import { ReloadIcon, Cross2Icon } from "@radix-ui/react-icons";
import { stringify as convertToYAML } from "yaml";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { WorkflowCreateYAMLRequest } from "@/routes/workflows/types/workflowYamlTypes";
import { toast } from "@/components/ui/use-toast";

interface ChatMessage {
  id: string;
  sender: "ai" | "user";
  content: string;
  timestamp?: string;
}

const formatChatTimestamp = (value: string) => {
  let normalizedValue = value.replace(/\.(\d{3})\d*/, ".$1");
  if (!normalizedValue.endsWith("Z")) {
    normalizedValue += "Z";
  }
  return new Date(normalizedValue).toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
  });
};

const MessageItem = memo(({ message }: { message: ChatMessage }) => {
  return (
    <div className="flex items-start gap-3">
      <div
        className={`flex h-8 w-8 items-center justify-center rounded-full text-xs font-bold text-white ${
          message.sender === "ai" ? "bg-blue-600" : "bg-purple-600"
        }`}
      >
        {message.sender === "ai" ? "AI" : "U"}
      </div>
      <div className="relative flex-1 rounded-lg bg-slate-800 p-3 pr-12">
        <p className="text-sm text-slate-200">{message.content}</p>
        {message.timestamp ? (
          <span className="pointer-events-none absolute bottom-2 right-2 rounded bg-slate-900/70 px-1.5 py-0.5 text-[10px] text-slate-400">
            {formatChatTimestamp(message.timestamp)}
          </span>
        ) : null}
      </div>
    </div>
  );
});

interface WorkflowCopilotChatProps {
  onWorkflowUpdate?: (workflowYaml: string) => void;
  isOpen?: boolean;
  onClose?: () => void;
  onMessageCountChange?: (count: number) => void;
  buttonRef?: React.RefObject<HTMLButtonElement>;
}

const DEFAULT_WINDOW_WIDTH = 600;
const DEFAULT_WINDOW_HEIGHT = 400;
const MIN_WINDOW_WIDTH = 300;
const MIN_WINDOW_HEIGHT = 300;
const OFFSET = 24;

const calculateDefaultPosition = (
  width: number,
  height: number,
  buttonRef?: React.RefObject<HTMLButtonElement>,
) => {
  // If button ref is available, align left edge of window with left edge of button
  if (buttonRef?.current) {
    const buttonRect = buttonRef.current.getBoundingClientRect();
    return {
      x: buttonRect.left - OFFSET,
      y: window.innerHeight - height - 2 * OFFSET,
    };
  }

  // Fallback to centered position
  return {
    x: window.innerWidth / 2 - width / 2,
    y: window.innerHeight - height - 2 * OFFSET,
  };
};

const constrainPosition = (
  x: number,
  y: number,
  width: number,
  height: number,
) => {
  const maxX = window.innerWidth - width - OFFSET;
  const maxY = window.innerHeight - height - OFFSET;

  return {
    x: Math.min(Math.max(0, x), maxX),
    y: Math.min(Math.max(0, y), maxY),
  };
};

export function WorkflowCopilotChat({
  onWorkflowUpdate,
  isOpen = true,
  onClose,
  onMessageCountChange,
  buttonRef,
}: WorkflowCopilotChatProps = {}) {
  const isSkyvernUser = useIsSkyvernUser();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [workflowCopilotChatId, setWorkflowCopilotChatId] = useState<
    string | null
  >(null);
  const [size, setSize] = useState({
    width: DEFAULT_WINDOW_WIDTH,
    height: DEFAULT_WINDOW_HEIGHT,
  });
  const [position, setPosition] = useState(
    calculateDefaultPosition(
      DEFAULT_WINDOW_WIDTH,
      DEFAULT_WINDOW_HEIGHT,
      buttonRef,
    ),
  );
  const [isDragging, setIsDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });
  const [isResizing, setIsResizing] = useState(false);
  const [resizeDirection, setResizeDirection] = useState<
    "n" | "s" | "e" | "w" | "se" | "sw" | "ne" | "nw"
  >("se");
  const [resizeStart, setResizeStart] = useState({
    x: 0,
    y: 0,
    width: 0,
    height: 0,
    posX: 0,
    posY: 0,
  });
  const credentialGetter = useCredentialGetter();
  const { workflowRunId, workflowPermanentId } = useParams();
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const { getSaveData } = useWorkflowHasChangesStore();
  const hasInitializedPosition = useRef(false);
  const hasScrolledOnLoad = useRef(false);

  const scrollToBottom = (behavior: ScrollBehavior) => {
    messagesEndRef.current?.scrollIntoView({ behavior });
  };

  const handleNewChat = () => {
    setMessages([]);
    setWorkflowCopilotChatId(null);
    hasScrolledOnLoad.current = false;
  };

  // Notify parent of message count changes
  useEffect(() => {
    if (onMessageCountChange) {
      onMessageCountChange(messages.length);
    }
  }, [messages.length, onMessageCountChange]);

  useEffect(() => {
    if (isLoadingHistory) {
      return;
    }
    if (!hasScrolledOnLoad.current) {
      scrollToBottom("auto");
      hasScrolledOnLoad.current = true;
      return;
    }
    scrollToBottom("smooth");
  }, [messages, isLoading, isLoadingHistory]);

  useEffect(() => {
    if (!workflowPermanentId) {
      setMessages([]);
      setWorkflowCopilotChatId(null);
      return;
    }

    let isMounted = true;

    const fetchHistory = async () => {
      setIsLoadingHistory(true);
      hasScrolledOnLoad.current = false;
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.get<{
          workflow_copilot_chat_id: string | null;
          chat_history: Array<{
            sender: "ai" | "user";
            content: string;
            created_at: string;
          }>;
        }>("/workflow/copilot/chat-history", {
          params: { workflow_permanent_id: workflowPermanentId },
        });

        if (!isMounted) return;

        const historyMessages = response.data.chat_history.map(
          (message, index) => ({
            id: `${index}-${Date.now()}`,
            sender: message.sender,
            content: message.content,
            timestamp: message.created_at,
          }),
        );
        setMessages(historyMessages);
        setWorkflowCopilotChatId(response.data.workflow_copilot_chat_id);
      } catch (error) {
        console.error("Failed to load chat history:", error);
      } finally {
        if (isMounted) {
          setIsLoadingHistory(false);
        }
      }
    };

    fetchHistory();

    return () => {
      isMounted = false;
    };
  }, [credentialGetter, workflowPermanentId]);

  const handleSend = async () => {
    if (!inputValue.trim() || isLoading) return;
    if (!workflowPermanentId) {
      toast({
        title: "Missing workflow",
        description: "Workflow permanent ID is required to chat.",
        variant: "destructive",
      });
      return;
    }

    const userMessageId = Date.now().toString();
    const userMessage: ChatMessage = {
      id: userMessageId,
      sender: "user",
      content: inputValue,
    };

    setMessages((prev) => [...prev, userMessage]);
    const messageContent = inputValue;
    setInputValue("");
    setIsLoading(true);

    try {
      const saveData = getSaveData();
      let workflowYaml = "";

      if (saveData) {
        const extraHttpHeaders: Record<string, string> = {};
        if (saveData.settings.extraHttpHeaders) {
          try {
            const parsedHeaders = JSON.parse(
              saveData.settings.extraHttpHeaders,
            );
            if (
              parsedHeaders &&
              typeof parsedHeaders === "object" &&
              !Array.isArray(parsedHeaders)
            ) {
              for (const [key, value] of Object.entries(parsedHeaders)) {
                if (key && typeof key === "string") {
                  extraHttpHeaders[key] = String(value);
                }
              }
            }
          } catch (error) {
            console.error("Error parsing extra HTTP headers:", error);
          }
        }

        const scriptCacheKey = saveData.settings.scriptCacheKey ?? "";
        const normalizedKey =
          scriptCacheKey === "" ? "default" : saveData.settings.scriptCacheKey;

        const requestBody: WorkflowCreateYAMLRequest = {
          title: saveData.title,
          description: saveData.workflow.description,
          proxy_location: saveData.settings.proxyLocation,
          webhook_callback_url: saveData.settings.webhookCallbackUrl,
          persist_browser_session: saveData.settings.persistBrowserSession,
          model: saveData.settings.model,
          max_screenshot_scrolls: saveData.settings.maxScreenshotScrolls,
          totp_verification_url: saveData.workflow.totp_verification_url,
          extra_http_headers: extraHttpHeaders,
          run_with: saveData.settings.runWith,
          cache_key: normalizedKey,
          ai_fallback: saveData.settings.aiFallback ?? true,
          workflow_definition: {
            version: saveData.workflowDefinitionVersion,
            parameters: saveData.parameters,
            blocks: saveData.blocks,
          },
          is_saved_task: saveData.workflow.is_saved_task,
          status: saveData.workflow.status,
          run_sequentially: saveData.settings.runSequentially,
          sequential_key: saveData.settings.sequentialKey,
        };

        workflowYaml = convertToYAML(requestBody);
      }

      const client = await getClient(credentialGetter, "sans-api-v1");

      const response = await client.post<{
        workflow_copilot_chat_id: string;
        message: string;
        updated_workflow_yaml: string | null;
        request_time: string;
        response_time: string;
      }>(
        "/workflow/copilot/chat-post",
        {
          workflow_permanent_id: workflowPermanentId,
          workflow_copilot_chat_id: workflowCopilotChatId,
          workflow_run_id: workflowRunId,
          message: messageContent,
          workflow_yaml: workflowYaml,
        },
        {
          timeout: 300000,
        },
      );

      setWorkflowCopilotChatId(response.data.workflow_copilot_chat_id);

      const aiMessage: ChatMessage = {
        id: Date.now().toString(),
        sender: "ai",
        content: response.data.message || "I received your message.",
        timestamp: response.data.response_time,
      };

      setMessages((prev) => [
        ...prev.map((message) =>
          message.id === userMessageId
            ? {
                ...message,
                timestamp: response.data.request_time,
              }
            : message,
        ),
        aiMessage,
      ]);

      if (response.data.updated_workflow_yaml && onWorkflowUpdate) {
        try {
          onWorkflowUpdate(response.data.updated_workflow_yaml);
        } catch (updateError) {
          console.error("Failed to update workflow:", updateError);
          toast({
            title: "Update failed",
            description: "Failed to apply workflow changes. Please try again.",
            variant: "destructive",
          });
        }
      }
    } catch (error) {
      console.error("Failed to send message:", error);
      const errorMessage: ChatMessage = {
        id: Date.now().toString(),
        sender: "ai",
        content: "Sorry, I encountered an error. Please try again.",
      };
      setMessages((prev) => [...prev, errorMessage]);
    } finally {
      setIsLoading(false);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      handleSend();
    }
  };

  const handleMouseDown = (e: React.MouseEvent) => {
    setIsDragging(true);
    setDragStart({
      x: e.clientX - position.x,
      y: e.clientY - position.y,
    });
  };

  const handleResizeMouseDown = (
    e: React.MouseEvent,
    direction: "n" | "s" | "e" | "w" | "se" | "sw" | "ne" | "nw",
  ) => {
    e.preventDefault();
    e.stopPropagation();
    setIsResizing(true);
    setResizeDirection(direction);
    setResizeStart({
      x: e.clientX,
      y: e.clientY,
      width: size.width,
      height: size.height,
      posX: position.x,
      posY: position.y,
    });
  };

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (isDragging) {
        setPosition({
          x: e.clientX - dragStart.x,
          y: e.clientY - dragStart.y,
        });
      }
      if (isResizing) {
        const deltaX = e.clientX - resizeStart.x;
        const deltaY = e.clientY - resizeStart.y;

        let newWidth = resizeStart.width;
        let newHeight = resizeStart.height;
        let newX = resizeStart.posX;
        let newY = resizeStart.posY;

        // Corners
        if (resizeDirection === "se") {
          // Southeast: resize from bottom-right
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width + deltaX);
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height + deltaY);
        } else if (resizeDirection === "sw") {
          // Southwest: resize from bottom-left
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width - deltaX);
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height + deltaY);
          if (resizeStart.width - deltaX >= MIN_WINDOW_WIDTH) {
            newX = resizeStart.posX + deltaX;
          }
        } else if (resizeDirection === "ne") {
          // Northeast: resize from top-right
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width + deltaX);
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height - deltaY);
          if (resizeStart.height - deltaY >= MIN_WINDOW_HEIGHT) {
            newY = resizeStart.posY + deltaY;
          }
        } else if (resizeDirection === "nw") {
          // Northwest: resize from top-left
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width - deltaX);
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height - deltaY);
          if (resizeStart.width - deltaX >= MIN_WINDOW_WIDTH) {
            newX = resizeStart.posX + deltaX;
          }
          if (resizeStart.height - deltaY >= MIN_WINDOW_HEIGHT) {
            newY = resizeStart.posY + deltaY;
          }
        }
        // Edges
        else if (resizeDirection === "n") {
          // North: resize from top
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height - deltaY);
          if (resizeStart.height - deltaY >= MIN_WINDOW_HEIGHT) {
            newY = resizeStart.posY + deltaY;
          }
        } else if (resizeDirection === "s") {
          // South: resize from bottom
          newHeight = Math.max(MIN_WINDOW_HEIGHT, resizeStart.height + deltaY);
        } else if (resizeDirection === "e") {
          // East: resize from right
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width + deltaX);
        } else if (resizeDirection === "w") {
          // West: resize from left
          newWidth = Math.max(MIN_WINDOW_WIDTH, resizeStart.width - deltaX);
          if (resizeStart.width - deltaX >= MIN_WINDOW_WIDTH) {
            newX = resizeStart.posX + deltaX;
          }
        }

        setSize({
          width: newWidth,
          height: newHeight,
        });
        setPosition({
          x: newX,
          y: newY,
        });
      }
    };

    const handleMouseUp = () => {
      setIsDragging(false);
      setIsResizing(false);
    };

    if (isDragging || isResizing) {
      document.addEventListener("mousemove", handleMouseMove);
      document.addEventListener("mouseup", handleMouseUp);
    }

    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDragging, dragStart, isResizing, resizeStart, resizeDirection]);

  // Handle window resize to keep chat window within viewport
  useEffect(() => {
    const handleResize = () => {
      setPosition((prev) =>
        constrainPosition(prev.x, prev.y, size.width, size.height),
      );
    };

    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [size]);

  // Recalculate position when chat opens to align with button (only first time)
  useEffect(() => {
    if (isOpen && buttonRef?.current && !hasInitializedPosition.current) {
      const newPosition = calculateDefaultPosition(
        size.width,
        size.height,
        buttonRef,
      );
      setPosition(newPosition);
      hasInitializedPosition.current = true;
    }
  }, [isOpen, buttonRef, size.width, size.height]);

  // Only show to Skyvern users
  if (!isSkyvernUser || !isOpen) {
    return null;
  }

  return (
    <div
      className="fixed z-50 flex flex-col rounded-lg border border-slate-700 bg-slate-900 shadow-2xl"
      style={{
        left: `${position.x}px`,
        top: `${position.y}px`,
        width: `${size.width}px`,
        height: `${size.height}px`,
      }}
    >
      {/* Header */}
      <div
        className="flex cursor-move items-center justify-between border-b border-slate-700 px-4 py-2"
        onMouseDown={handleMouseDown}
      >
        <h3 className="text-sm font-semibold text-slate-200">
          Workflow Copilot
        </h3>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleNewChat}
            onMouseDown={(e) => e.stopPropagation()}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
          >
            New chat
          </button>
          <div className="h-2 w-2 rounded-full bg-green-500"></div>
          <span className="text-xs text-slate-400">Active</span>
          <button
            type="button"
            onClick={() => onClose?.()}
            onMouseDown={(e) => e.stopPropagation()}
            className="ml-2 rounded p-1 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
            title="Close"
          >
            <Cross2Icon className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4">
        <div className="space-y-3">
          {!isLoadingHistory && messages.length === 0 && !isLoading ? (
            <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-4 text-sm text-slate-300">
              <p className="font-semibold text-slate-200">Start a new chat</p>
              <p className="mt-2 text-slate-400">
                Ask the copilot to draft or edit your workflow. Provide a goal,
                the target site, and any credentials it should use.
              </p>
              <p className="mt-2 text-slate-400">
                Example: "Build workflow to find the top post on hackernews
                today"
              </p>
            </div>
          ) : null}
          {messages.map((message) => (
            <MessageItem key={message.id} message={message} />
          ))}
          {isLoading && (
            <div className="flex items-start gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-blue-600 text-xs font-bold text-white">
                AI
              </div>
              <div className="flex-1 rounded-lg bg-slate-800 p-3">
                <div className="flex items-center gap-2 text-sm text-slate-400">
                  <ReloadIcon className="h-4 w-4 animate-spin" />
                  <span>Processing...</span>
                </div>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Input */}
      <div className="border-t border-slate-700 p-3">
        <div className="flex gap-2">
          <input
            type="text"
            placeholder="Type your message..."
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyPress={handleKeyPress}
            disabled={isLoading}
            className="flex-1 rounded-md border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-slate-200 placeholder-slate-500 focus:border-blue-500 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50"
          />
          <button
            onClick={handleSend}
            disabled={isLoading}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Send
          </button>
        </div>
      </div>

      {/* Resize Handles */}
      {/* Corners */}
      <div
        className="absolute bottom-0 right-0 z-10 h-3 w-3 cursor-nwse-resize"
        onMouseDown={(e) => handleResizeMouseDown(e, "se")}
        title="Resize"
      />
      <div
        className="absolute bottom-0 left-0 z-10 h-3 w-3 cursor-nesw-resize"
        onMouseDown={(e) => handleResizeMouseDown(e, "sw")}
        title="Resize"
      />
      <div
        className="absolute right-0 top-0 z-10 h-3 w-3 cursor-nesw-resize"
        onMouseDown={(e) => handleResizeMouseDown(e, "ne")}
        title="Resize"
      />
      <div
        className="absolute left-0 top-0 z-10 h-3 w-3 cursor-nwse-resize"
        onMouseDown={(e) => handleResizeMouseDown(e, "nw")}
        title="Resize"
      />
      {/* Edges */}
      <div
        className="absolute left-3 right-3 top-0 z-10 h-1 cursor-ns-resize"
        onMouseDown={(e) => handleResizeMouseDown(e, "n")}
        title="Resize"
      />
      <div
        className="absolute bottom-0 left-3 right-3 z-10 h-1 cursor-ns-resize"
        onMouseDown={(e) => handleResizeMouseDown(e, "s")}
        title="Resize"
      />
      <div
        className="absolute bottom-3 left-0 top-3 z-10 w-1 cursor-ew-resize"
        onMouseDown={(e) => handleResizeMouseDown(e, "w")}
        title="Resize"
      />
      <div
        className="absolute bottom-3 right-0 top-3 z-10 w-1 cursor-ew-resize"
        onMouseDown={(e) => handleResizeMouseDown(e, "e")}
        title="Resize"
      />
    </div>
  );
}
