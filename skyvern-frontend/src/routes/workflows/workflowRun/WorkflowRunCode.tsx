import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { HelpTooltip } from "@/components/HelpTooltip";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { useCacheKeyValuesQuery } from "@/routes/workflows/hooks/useCacheKeyValuesQuery";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { constructCacheKeyValue } from "@/routes/workflows/editor/utils";
import { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";

interface Props {
  showCacheKeyValueSelector?: boolean;
}

const getOrderedBlockLabels = (workflow?: WorkflowApiResponse) => {
  if (!workflow) {
    return [];
  }

  const blockLabels = workflow.workflow_definition.blocks.map(
    (block) => block.label,
  );

  return blockLabels;
};

const getCommentForBlockWithoutCode = (blockLabel: string) => {
  return `
  # If the "Generate Code" option is turned on for this workflow when it runs, AI will execute block '${blockLabel}', and generate code for it.
`;
};

const getCode = (
  orderedBlockLabels: string[],
  blockScripts?: {
    [blockName: string]: string;
  },
): string[] => {
  const blockCode: string[] = [];
  const startBlockCode = blockScripts?.__start_block__;

  if (startBlockCode) {
    blockCode.push(startBlockCode);
  }

  for (const blockLabel of orderedBlockLabels) {
    const code = blockScripts?.[blockLabel];

    if (!code) {
      blockCode.push(getCommentForBlockWithoutCode(blockLabel));
      continue;
    }

    blockCode.push(`${code}
`);
  }

  return blockCode;
};

function WorkflowRunCode(props?: Props) {
  const showCacheKeyValueSelector = props?.showCacheKeyValueSelector ?? false;
  const queryClient = useQueryClient();
  const { workflowPermanentId } = useParams();
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });
  const cacheKey = workflow?.cache_key ?? "";
  const [cacheKeyValue, setCacheKeyValue] = useState(
    cacheKey === "" ? "" : constructCacheKeyValue(cacheKey, workflow),
  );
  const { data: cacheKeyValues } = useCacheKeyValuesQuery({
    cacheKey,
    debounceMs: 100,
    page: 1,
    workflowPermanentId,
  });

  useEffect(() => {
    setCacheKeyValue(
      cacheKeyValues?.values[0] ?? constructCacheKeyValue(cacheKey, workflow),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKeyValues, setCacheKeyValue, workflow]);

  useEffect(() => {
    queryClient.invalidateQueries({
      queryKey: [
        "cache-key-values",
        workflowPermanentId,
        cacheKey,
        1,
        undefined,
      ],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflow]);

  const { data: blockScripts } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
  });

  const orderedBlockLabels = getOrderedBlockLabels(workflow);
  const code = getCode(orderedBlockLabels, blockScripts).join("");

  if (code.length === 0) {
    return (
      <div className="flex items-center justify-center bg-slate-elevation3 p-8">
        No code has been generated yet.
      </div>
    );
  }

  if (
    !showCacheKeyValueSelector ||
    (cacheKeyValues?.values ?? []).length <= 1
  ) {
    return (
      <CodeEditor
        className="h-full overflow-y-scroll"
        language="python"
        value={code}
        lineWrap={false}
        readOnly
        fontSize={10}
      />
    );
  }

  return (
    <div className="flex h-full w-full flex-col items-end justify-center gap-2">
      <div className="flex w-[20rem] gap-4">
        <div className="flex items-center justify-around gap-2">
          <Label className="w-[7rem]">Code Cache Key</Label>
          <HelpTooltip content="Which generated (& cached) code to view." />
        </div>
        <Select
          value={cacheKeyValue}
          onValueChange={(v: string) => setCacheKeyValue(v)}
        >
          <SelectTrigger>
            <SelectValue placeholder="Code Key Value" />
          </SelectTrigger>
          <SelectContent>
            {(cacheKeyValues?.values ?? []).map((value) => {
              return (
                <SelectItem key={value} value={value}>
                  {value}
                </SelectItem>
              );
            })}
          </SelectContent>
        </Select>
      </div>
      <CodeEditor
        className="h-full w-full overflow-y-scroll"
        language="python"
        value={code}
        lineWrap={false}
        readOnly
        fontSize={10}
      />
    </div>
  );
}

export { WorkflowRunCode };
