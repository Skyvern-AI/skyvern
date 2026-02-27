import { useState, useRef } from "react";
import { PlusIcon } from "@radix-ui/react-icons";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useDebounce } from "use-debounce";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { WorkflowBlockParameterSelect } from "@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect";

interface WorkflowSelectorProps {
  nodeId: string;
  value: string;
  onChange: (value: string) => void;
}

function WorkflowSelector({ nodeId, value, onChange }: WorkflowSelectorProps) {
  const [focused, setFocused] = useState(false);
  const [debouncedValue] = useDebounce(value, 300);
  const credentialGetter = useCredentialGetter();
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const isTyping = value !== debouncedValue;

  const { data: workflows = [], isFetching } = useQuery<
    Array<WorkflowApiResponse>
  >({
    queryKey: ["workflows", "selector", debouncedValue],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", "1");
      params.append("page_size", "10");
      params.append("only_workflows", "true");
      if (debouncedValue) {
        params.append("search_key", debouncedValue);
      }
      return client
        .get("/workflows", { params })
        .then((response) => response.data);
    },
    enabled: focused,
  });

  const showDropdown =
    focused && (workflows.length > 0 || isFetching || isTyping);

  const insertParameter = (parameterKey: string) => {
    const parameterText = `{{${parameterKey}}}`;
    const input = inputRef.current;
    if (input) {
      const start = input.selectionStart ?? value.length;
      const end = input.selectionEnd ?? value.length;
      const newValue =
        value.substring(0, start) + parameterText + value.substring(end);
      onChange(newValue);
      setTimeout(() => {
        const newPosition = start + parameterText.length;
        input.focus();
        input.setSelectionRange(newPosition, newPosition);
      }, 0);
    } else {
      onChange(`${value}${parameterText}`);
    }
  };

  return (
    <div
      ref={containerRef}
      className="nopan relative"
      onBlur={(e) => {
        if (!containerRef.current?.contains(e.relatedTarget as Node)) {
          setFocused(false);
        }
      }}
    >
      <input
        ref={inputRef}
        id={`workflow-selector-${nodeId}`}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setFocused(true)}
        placeholder="Search by title or enter wpid_xxx / {{ parameter }}"
        className="w-full rounded-md border border-input bg-transparent px-3 py-2 pr-9 text-xs text-slate-300 shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      />
      <div className="absolute right-1 top-0 flex size-9 items-center justify-end">
        <Popover>
          <PopoverTrigger asChild>
            <div className="cursor-pointer rounded p-1 hover:bg-muted">
              <PlusIcon className="size-4" />
            </div>
          </PopoverTrigger>
          <PopoverContent className="w-[22rem]">
            <WorkflowBlockParameterSelect
              nodeId={nodeId}
              onAdd={insertParameter}
            />
          </PopoverContent>
        </Popover>
      </div>
      {showDropdown && (
        <div className="absolute z-50 mt-1 w-full overflow-hidden rounded-md border border-slate-600 bg-slate-800 shadow-lg">
          <div className="max-h-[200px] overflow-y-auto">
            {(isFetching || isTyping) && workflows.length === 0 ? (
              <>
                {Array.from({ length: 3 }).map((_, index) => (
                  <div
                    key={`skeleton-${index}`}
                    className="flex w-full flex-col gap-1 px-3 py-2"
                  >
                    <Skeleton className="h-3.5 w-3/4" />
                    <Skeleton className="h-3 w-1/2" />
                  </div>
                ))}
              </>
            ) : (
              workflows.map((workflow) => {
                const isSelected = value === workflow.workflow_permanent_id;
                return (
                  <button
                    key={workflow.workflow_permanent_id}
                    type="button"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => {
                      onChange(workflow.workflow_permanent_id);
                      setFocused(false);
                    }}
                    className={`flex w-full flex-col gap-0.5 px-3 py-2 text-left text-xs transition-colors hover:bg-slate-700 ${
                      isSelected ? "bg-slate-700" : ""
                    }`}
                  >
                    <span className="font-medium text-slate-200">
                      {workflow.title}
                    </span>
                    <span className="text-slate-500">
                      {workflow.workflow_permanent_id}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export { WorkflowSelector };
