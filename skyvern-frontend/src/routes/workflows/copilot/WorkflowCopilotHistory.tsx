import { useEffect, useMemo, useState } from "react";
import {
  CheckIcon,
  CounterClockwiseClockIcon,
  MagnifyingGlassIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Skeleton } from "@/components/ui/skeleton";
import { handleInfiniteScroll } from "@/util/utils";
import { compactLocalDateTime } from "@/util/timeFormat";
import { useDebounce } from "use-debounce";
import { useInfiniteCopilotChatsQuery } from "./useInfiniteCopilotChatsQuery";
import { WorkflowCopilotChatSummary } from "./workflowCopilotTypes";

interface WorkflowCopilotHistoryProps {
  workflowPermanentId: string | undefined;
  currentChatId: string | null;
  onSelect: (chat: WorkflowCopilotChatSummary) => void;
  disabled?: boolean;
  // Icon-only trigger (narrow pane headers); the label moves to a tooltip.
  compact?: boolean;
}

interface WorkflowCopilotHistoryContentProps {
  workflowPermanentId: string | undefined;
  currentChatId: string | null;
  onSelect: (chat: WorkflowCopilotChatSummary) => void;
}

// Lives inside PopoverContent so the react-query subscription only mounts while
// the dropdown is open.
function WorkflowCopilotHistoryContent({
  workflowPermanentId,
  currentChatId,
  onSelect,
}: WorkflowCopilotHistoryContentProps) {
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const isTyping = search !== debouncedSearch;

  const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isFetching } =
    useInfiniteCopilotChatsQuery({
      workflow_permanent_id: workflowPermanentId,
      search: debouncedSearch,
      page_size: 20,
    });

  const chats = useMemo(
    () => data?.pages.flatMap((page) => page) ?? [],
    [data],
  );

  return (
    <>
      <div className="border-b p-3">
        <h4 className="mb-2 text-sm font-medium">Chat history</h4>
        <div className="relative">
          <MagnifyingGlassIcon className="absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <Input
            placeholder="Search chats..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-8 pl-8"
            autoFocus
          />
        </div>
      </div>
      <div
        className="max-h-[300px] overflow-y-auto overflow-x-hidden [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:border-2 [&::-webkit-scrollbar-thumb]:border-slate-100 [&::-webkit-scrollbar-thumb]:bg-slate-300 dark:[&::-webkit-scrollbar-thumb]:border-slate-800 dark:[&::-webkit-scrollbar-thumb]:bg-slate-600 [&::-webkit-scrollbar-track]:bg-slate-100 dark:[&::-webkit-scrollbar-track]:bg-slate-800 [&::-webkit-scrollbar]:w-2"
        onScroll={(e) =>
          handleInfiniteScroll(
            e,
            fetchNextPage,
            hasNextPage,
            isFetchingNextPage,
          )
        }
      >
        {(isFetching || isTyping) && chats.length === 0 ? (
          <>
            {Array.from({ length: 8 }).map((_, index) => (
              <div
                key={`skeleton-${index}`}
                className="flex w-full flex-col gap-1 px-3 py-2"
              >
                <Skeleton className="h-4 w-3/4" />
                <Skeleton className="h-3 w-1/2" />
              </div>
            ))}
          </>
        ) : chats.length === 0 ? (
          <div className="px-3 py-8 text-center text-sm text-slate-400">
            No chats found
          </div>
        ) : (
          <>
            {chats.map((chat) => {
              const isCurrent = currentChatId === chat.workflow_copilot_chat_id;
              return (
                <button
                  key={chat.workflow_copilot_chat_id}
                  type="button"
                  onClick={() => onSelect(chat)}
                  className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-slate-50 dark:hover:bg-slate-800"
                >
                  <div className="flex min-w-0 flex-col">
                    <span className="line-clamp-2 break-words [overflow-wrap:anywhere]">
                      {chat.title || "Untitled chat"}
                    </span>
                    <span className="truncate text-xs text-slate-400">
                      {compactLocalDateTime(chat.created_at)}
                    </span>
                  </div>
                  {isCurrent && (
                    <CheckIcon className="h-4 w-4 shrink-0 text-blue-400" />
                  )}
                </button>
              );
            })}
            {isFetchingNextPage && (
              <div className="flex items-center justify-center py-2">
                <ReloadIcon className="h-3 w-3 animate-spin text-slate-400" />
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}

function WorkflowCopilotHistory({
  workflowPermanentId,
  currentChatId,
  onSelect,
  disabled = false,
  compact = false,
}: WorkflowCopilotHistoryProps) {
  const [open, setOpen] = useState(false);

  // Close the dropdown when the copilot starts loading so a selection can't
  // race an in-flight turn or chat load.
  useEffect(() => {
    if (disabled) {
      setOpen(false);
    }
  }, [disabled]);

  function handleSelect(chat: WorkflowCopilotChatSummary) {
    setOpen(false);
    onSelect(chat);
  }

  const trigger = (
    <PopoverTrigger asChild>
      <button
        type="button"
        disabled={disabled}
        aria-label="History"
        onMouseDown={(e) => e.stopPropagation()}
        className={
          compact
            ? "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
            : "flex items-center gap-1 rounded border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
        }
      >
        <CounterClockwiseClockIcon
          className={compact ? "h-3.5 w-3.5" : "h-3 w-3"}
          aria-hidden="true"
        />
        {compact ? null : "History"}
      </button>
    </PopoverTrigger>
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      {compact ? (
        <Tooltip>
          <TooltipTrigger asChild>{trigger}</TooltipTrigger>
          <TooltipContent side="bottom">History</TooltipContent>
        </Tooltip>
      ) : (
        trigger
      )}
      <PopoverContent
        className="w-80 p-0"
        align="end"
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <WorkflowCopilotHistoryContent
          workflowPermanentId={workflowPermanentId}
          currentChatId={currentChatId}
          onSelect={handleSelect}
        />
      </PopoverContent>
    </Popover>
  );
}

export { WorkflowCopilotHistory };
