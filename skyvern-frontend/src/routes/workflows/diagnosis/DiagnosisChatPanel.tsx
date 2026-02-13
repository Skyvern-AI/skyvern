import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  ChatBubbleIcon,
  PaperPlaneIcon,
  ExclamationTriangleIcon,
  ReloadIcon,
  StopIcon,
} from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { toast } from "@/components/ui/use-toast";
import { cn } from "@/util/utils";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getSseClient } from "@/api/sse";
import { useDiagnosisChatHistoryQuery } from "../hooks/useDiagnosisChatQuery";
import type {
  DiagnosisChatHistoryMessage,
  DiagnosisStreamMessage,
} from "@/api/types";

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  timestamp?: Date;
  isStreaming?: boolean;
};

type DiagnosisChatPanelProps = {
  workflowRunId: string;
  className?: string;
};

const QUICK_PROMPTS = [
  "What went wrong?",
  "How can I fix this?",
  "What were the actions taken?",
  "Show me the error details",
];

export function DiagnosisChatPanel({
  workflowRunId,
  className,
}: DiagnosisChatPanelProps) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const scrollViewportRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [processingStatus, setProcessingStatus] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);

  const { data: history, isLoading: historyLoading } =
    useDiagnosisChatHistoryQuery({
      workflowRunId,
    });

  // Load history on mount
  useEffect(() => {
    if (history) {
      setConversationId(history.diagnosis_conversation_id);
      setMessages(
        history.messages.map((msg: DiagnosisChatHistoryMessage) => ({
          role: msg.role as "user" | "assistant",
          content: msg.content,
          timestamp: new Date(msg.created_at),
        })),
      );
    }
  }, [history]);

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    if (scrollViewportRef.current) {
      scrollViewportRef.current.scrollTop =
        scrollViewportRef.current.scrollHeight;
    }
  }, [messages, processingStatus]);

  const sendMessage = useCallback(
    async (message: string) => {
      if (!message.trim() || isStreaming) return;

      // Add user message immediately
      const userMessage: ChatMessage = {
        role: "user",
        content: message,
        timestamp: new Date(),
      };
      setMessages((prev) => [...prev, userMessage]);
      setInputValue("");
      setIsStreaming(true);
      setProcessingStatus("Connecting...");

      // Create abort controller for this request
      abortControllerRef.current = new AbortController();

      // Add placeholder for assistant response
      const assistantMessage: ChatMessage = {
        role: "assistant",
        content: "",
        isStreaming: true,
      };
      setMessages((prev) => [...prev, assistantMessage]);

      try {
        const sseClient = await getSseClient(credentialGetter);

        await sseClient.postStreaming<DiagnosisStreamMessage>(
          `/v1/workflow_runs/${workflowRunId}/diagnosis/chat`,
          {
            message,
            diagnosis_conversation_id: conversationId,
          },
          (payload) => {
            switch (payload.type) {
              case "processing":
                setProcessingStatus(payload.status);
                break;

              case "content":
                setProcessingStatus(null);
                setMessages((prev) => {
                  const newMessages = [...prev];
                  const lastMessage = newMessages[newMessages.length - 1];
                  if (lastMessage && lastMessage.role === "assistant") {
                    lastMessage.content = payload.content;
                  }
                  return newMessages;
                });
                break;

              case "complete":
                setConversationId(payload.diagnosis_conversation_id);
                setMessages((prev) => {
                  const newMessages = [...prev];
                  const lastMessage = newMessages[newMessages.length - 1];
                  if (lastMessage && lastMessage.role === "assistant") {
                    lastMessage.content = payload.full_response;
                    lastMessage.isStreaming = false;
                    lastMessage.timestamp = new Date(payload.timestamp);
                  }
                  return newMessages;
                });
                // Invalidate the history query to keep it in sync
                queryClient.invalidateQueries({
                  queryKey: ["diagnosisChat", workflowRunId],
                });
                return true; // Signal completion

              case "error":
                toast({
                  title: "Error",
                  description: payload.error,
                  variant: "destructive",
                });
                // Remove the streaming assistant message on error
                setMessages((prev) => prev.filter((m) => !m.isStreaming));
                return true; // Signal completion
            }
            return false;
          },
          { signal: abortControllerRef.current.signal },
        );
      } catch (error) {
        if ((error as Error).name !== "AbortError") {
          toast({
            title: "Error",
            description: "Failed to send message. Please try again.",
            variant: "destructive",
          });
          // Remove the streaming assistant message on error
          setMessages((prev) => prev.filter((m) => !m.isStreaming));
        }
      } finally {
        setIsStreaming(false);
        setProcessingStatus(null);
        abortControllerRef.current = null;
      }
    },
    [workflowRunId, conversationId, credentialGetter, isStreaming, queryClient],
  );

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      sendMessage(inputValue);
    },
    [inputValue, sendMessage],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage(inputValue);
      }
    },
    [inputValue, sendMessage],
  );

  const handleCancel = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
  }, []);

  if (historyLoading) {
    return (
      <div className={cn("flex items-center justify-center p-8", className)}>
        <ReloadIcon className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className={cn("flex h-full flex-col bg-slate-elevation1", className)}>
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-slate-700 px-4 py-3">
        <ChatBubbleIcon className="h-5 w-5 text-primary" />
        <h3 className="text-sm font-medium">Diagnose Run</h3>
        {history?.status === "escalated" && (
          <span className="ml-auto flex items-center gap-1 text-xs text-yellow-500">
            <ExclamationTriangleIcon className="h-3 w-3" />
            Escalated
          </span>
        )}
      </div>

      {/* Messages */}
      <ScrollArea className="flex-1">
        <ScrollAreaViewport ref={scrollViewportRef} className="h-full p-4">
          {messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center text-muted-foreground">
              <ChatBubbleIcon className="mb-4 h-12 w-12 opacity-50" />
              <p className="mb-4 text-sm">
                Ask about this workflow run to understand what happened.
              </p>
              <div className="flex flex-wrap justify-center gap-2">
                {QUICK_PROMPTS.map((prompt) => (
                  <Button
                    key={prompt}
                    variant="outline"
                    size="sm"
                    onClick={() => sendMessage(prompt)}
                    disabled={isStreaming}
                  >
                    {prompt}
                  </Button>
                ))}
              </div>
            </div>
          ) : (
            <div className="space-y-4">
              {messages.map((message, index) => (
                <div
                  key={index}
                  className={cn(
                    "flex max-w-[85%] flex-col rounded-lg p-3",
                    message.role === "user"
                      ? "ml-auto bg-primary text-primary-foreground"
                      : "bg-slate-elevation3",
                  )}
                >
                  <div className="whitespace-pre-wrap text-sm">
                    {message.content || (
                      <span className="italic text-muted-foreground">
                        {processingStatus || "Thinking..."}
                      </span>
                    )}
                    {message.isStreaming && (
                      <span className="ml-1 inline-block h-4 w-2 animate-pulse bg-current" />
                    )}
                  </div>
                  {message.timestamp && !message.isStreaming && (
                    <span className="mt-1 text-xs opacity-60">
                      {message.timestamp.toLocaleTimeString()}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </ScrollAreaViewport>
      </ScrollArea>

      {/* Processing indicator */}
      {processingStatus && (
        <div className="flex items-center gap-2 border-t border-slate-700 px-4 py-2 text-xs text-muted-foreground">
          <ReloadIcon className="h-3 w-3 animate-spin" />
          {processingStatus}
        </div>
      )}

      {/* Input */}
      <form onSubmit={handleSubmit} className="border-t border-slate-700 p-4">
        <div className="flex gap-2">
          <Textarea
            ref={textareaRef}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about this run..."
            disabled={isStreaming}
            className="max-h-[120px] min-h-[60px] resize-none"
            rows={2}
          />
          {isStreaming ? (
            <Button
              type="button"
              variant="outline"
              size="icon"
              onClick={handleCancel}
              className="h-[60px] w-[60px]"
            >
              <span className="sr-only">Cancel</span>
              <StopIcon className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              disabled={!inputValue.trim()}
              className="h-[60px] w-[60px]"
            >
              <PaperPlaneIcon className="h-4 w-4" />
              <span className="sr-only">Send</span>
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}
