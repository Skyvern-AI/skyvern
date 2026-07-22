import { CodeIcon } from "@radix-ui/react-icons";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import {
  DropdownMenuCheckboxItem,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";
import { useCacheKeyValuesQuery } from "@/routes/workflows/hooks/useCacheKeyValuesQuery";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useShowAllCodeStore } from "@/store/ShowAllCodeStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import { useIsGeneratingCode } from "../hooks/useIsGeneratingCode";
import { useToggleCodeView } from "../hooks/useToggleCodeView";

/**
 * Code controls (show/hide the generated code + pick which cached variant to
 * view) live in the overflow menu rather than the main toolbar — they only
 * matter once a run has produced cached code, which is rare and advanced.
 * Renders nothing until that's the case, mirroring the old inline controls.
 */
function CodeSubmenu() {
  const workflowPermanentId = useWorkflowPermanentId();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const cacheKey = workflow?.cache_key ?? "";

  const isDebugMode = useDebugStore().isDebugMode;
  const isRecording = useRecordingStore((s) => s.isRecording);
  const showAllCode = useShowAllCodeStore((s) => s.showAllCode);
  const toggleCodeView = useToggleCodeView();

  const cacheKeyValue = useCacheKeyValueStore((s) => s.cacheKeyValue);
  const setExplicitCacheKeyValue = useCacheKeyValueStore((s) => s.setExplicit);
  const setWorkflowPanelState = useWorkflowPanelStore(
    (s) => s.setWorkflowPanelState,
  );

  const isGeneratingCode = useIsGeneratingCode({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
  });

  const { data: cacheKeyValues } = useCacheKeyValuesQuery({
    cacheKey,
    page: 1,
    workflowPermanentId,
  });

  const values = cacheKeyValues?.values ?? [];
  const totalCount = cacheKeyValues?.total_count ?? 0;

  if (isRecording || isGeneratingCode || totalCount === 0) {
    return null;
  }

  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger>
        <CodeIcon className="mr-2 size-4" />
        Code
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent className="max-h-80 overflow-y-auto">
        {isDebugMode && (
          <>
            <DropdownMenuCheckboxItem
              checked={showAllCode}
              onCheckedChange={() => toggleCodeView()}
              onSelect={(event) => event.preventDefault()}
            >
              Show code
            </DropdownMenuCheckboxItem>
            <DropdownMenuSeparator />
          </>
        )}
        <DropdownMenuRadioGroup
          value={cacheKeyValue || undefined}
          onValueChange={(value) => setExplicitCacheKeyValue(value)}
        >
          {values.map((value) => (
            <DropdownMenuRadioItem key={value} value={value}>
              {value}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={() =>
            setWorkflowPanelState({
              active: true,
              content: "cacheKeyValues",
            })
          }
        >
          Search or add value…
        </DropdownMenuItem>
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  );
}

export { CodeSubmenu };
