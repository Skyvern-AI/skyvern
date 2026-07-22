import { useCallback, useEffect, useMemo, useRef } from "react";
import { useNodes, useReactFlow } from "@xyflow/react";
import {
  PlusIcon,
  ChevronDownIcon,
  DotsVerticalIcon,
} from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/util/utils";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useUpdate } from "../../useUpdate";
import { AppNode, isWorkflowBlockNode } from "..";
import { updateNodeAndDescendantsVisibility } from "../../workflowEditorUtils";
import { applyEdgeVisibility } from "./applyEdgeVisibility";
import {
  getConditionLabel,
  orderBranchesWithDefaultsLast,
} from "./branchDisplayUtils";
import {
  ConditionalNodeData,
  createBranchCondition,
  defaultBranchCriteria,
} from "./types";
import type { BranchCondition } from "../../../types/workflowTypes";
import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";

type Props = {
  nodeId: string;
  data: ConditionalNodeData;
};

function BranchesEditor({ nodeId, data }: Props) {
  const id = nodeId;
  const nodes = useNodes<AppNode>();
  const { setNodes, setEdges } = useReactFlow();
  const { beginInternalUpdate, endInternalUpdate } =
    useWorkflowHasChangesStore();
  // Track pending endInternalUpdate timer from handleSelectBranch so we can
  // clean it up on unmount and prevent a stuck counter.
  const branchSelectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  useEffect(() => {
    return () => {
      if (branchSelectTimerRef.current !== null) {
        clearTimeout(branchSelectTimerRef.current);
        endInternalUpdate();
        branchSelectTimerRef.current = null;
      }
    };
  }, [endInternalUpdate]);

  const update = useUpdate<ConditionalNodeData>({
    id,
    editable: data.editable,
  });

  const orderedBranches = useMemo(
    () => orderBranchesWithDefaultsLast(data.branches),
    [data.branches],
  );

  const activeBranch =
    orderedBranches.find((branch) => branch.id === data.activeBranchId) ??
    orderedBranches[0] ??
    null;

  useEffect(() => {
    if (!data.branches.some((branch) => branch.is_default)) {
      update({
        branches: [
          ...data.branches,
          createBranchCondition({ is_default: true }),
        ],
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.branches]);

  useEffect(() => {
    if (!data.activeBranchId && orderedBranches.length > 0) {
      // Mark as internal update to prevent triggering "unsaved changes" dialog
      beginInternalUpdate();
      update({
        activeBranchId: orderedBranches[0]?.id ?? null,
      });
      // Clear the flag after layout completes
      let ended = false;
      const timer = setTimeout(() => {
        ended = true;
        endInternalUpdate();
      }, 50);
      return () => {
        clearTimeout(timer);
        if (!ended) {
          endInternalUpdate();
        }
      };
    }
  }, [
    data.activeBranchId,
    orderedBranches,
    update,
    beginInternalUpdate,
    endInternalUpdate,
  ]);

  // Toggle visibility of branch nodes/edges when activeBranchId changes
  useEffect(() => {
    if (!data.activeBranchId) {
      return;
    }

    // Mark as internal update to prevent triggering "unsaved changes" dialog
    beginInternalUpdate();

    const activeBranchId = data.activeBranchId;
    let updatedNodesSnapshot: Array<AppNode> = [];

    // Toggle node visibility with cascading to descendants
    setNodes((currentNodes) => {
      let updatedNodes = currentNodes as Array<AppNode>;

      // First pass: Update direct children of this conditional
      updatedNodes = updatedNodes.map((n) => {
        // Only affect workflow block nodes that belong to this conditional
        if (!isWorkflowBlockNode(n)) {
          return n;
        }
        if (n.data.conditionalNodeId !== id) {
          return n;
        }
        if (!n.data.conditionalBranchId) {
          return n;
        }

        // Hide nodes that don't match the active branch
        const shouldHide = n.data.conditionalBranchId !== activeBranchId;
        return { ...n, hidden: shouldHide };
      });

      // Second pass: Cascade visibility to all descendants of affected nodes
      const affectedNodeIds = updatedNodes
        .filter(isWorkflowBlockNode)
        .filter((n) => n.data.conditionalNodeId === id)
        .map((n) => n.id);

      affectedNodeIds.forEach((nodeId) => {
        const node = updatedNodes.find((n) => n.id === nodeId);
        if (node) {
          updatedNodes = updateNodeAndDescendantsVisibility(
            updatedNodes,
            nodeId,
            node.hidden ?? false,
          );
        }
      });

      updatedNodesSnapshot = updatedNodes;
      return updatedNodes;
    });

    setEdges((currentEdges) => {
      const nodeMap = new Map(updatedNodesSnapshot.map((n) => [n.id, n]));
      return currentEdges.map((edge) =>
        applyEdgeVisibility(edge, nodeMap, id, activeBranchId),
      );
    });

    // Trigger layout recalculation after visibility changes
    setTimeout(() => {
      window.dispatchEvent(new CustomEvent("conditional-branch-changed"));
    }, 0);

    // Clear the internal update flag after changes propagate
    let ended = false;
    const timer = setTimeout(() => {
      ended = true;
      endInternalUpdate();
    }, 50);
    return () => {
      clearTimeout(timer);
      if (!ended) {
        endInternalUpdate();
      }
    };
  }, [
    data.activeBranchId,
    id,
    setNodes,
    setEdges,
    beginInternalUpdate,
    endInternalUpdate,
  ]);

  const handleAddCondition = () => {
    if (!data.editable) {
      return;
    }
    const defaultBranches = data.branches.filter((branch) => branch.is_default);
    const otherBranches = data.branches.filter((branch) => !branch.is_default);
    const newBranch = createBranchCondition();
    const updatedBranches = [...otherBranches, newBranch, ...defaultBranches];

    // Find the START and NodeAdder nodes inside this conditional
    const startNode = nodes.find(
      (n) => n.type === "start" && n.parentId === id,
    );
    const adderNode = nodes.find(
      (n) => n.type === "nodeAdder" && n.parentId === id,
    );

    // Create a START → NodeAdder edge for the new branch
    if (startNode && adderNode) {
      setEdges((currentEdges) => [
        ...currentEdges,
        {
          id: `${id}-${newBranch.id}-start-adder`,
          type: "default",
          source: startNode.id,
          target: adderNode.id,
          style: { strokeWidth: 2 },
          data: {
            conditionalNodeId: id,
            conditionalBranchId: newBranch.id,
          },
          hidden: false, // This branch will be active
        },
      ]);
    }

    update({
      branches: updatedBranches,
      activeBranchId: newBranch.id,
    });
  };

  const handleSelectBranch = useCallback(
    (branchId: string) => {
      if (!data.editable) {
        return;
      }
      // Mark as internal update to prevent triggering "unsaved changes" dialog
      // Switching branches is UI state, not actual workflow data changes
      // Cancel any pending timer from a previous rapid call to keep the counter balanced
      if (branchSelectTimerRef.current !== null) {
        clearTimeout(branchSelectTimerRef.current);
        endInternalUpdate();
      }
      beginInternalUpdate();
      update({ activeBranchId: branchId });
      // Clear the flag after layout completes (layout uses setTimeout(10))
      // Store timer in ref so it can be cleaned up on unmount
      branchSelectTimerRef.current = setTimeout(() => {
        branchSelectTimerRef.current = null;
        endInternalUpdate();
      }, 50);
    },
    [data.editable, beginInternalUpdate, update, endInternalUpdate],
  );

  const handleRemoveBranch = (branchId: string) => {
    if (!data.editable) {
      return;
    }

    // Don't allow removing if it's the last non-default branch
    const nonDefaultBranches = data.branches.filter((b) => !b.is_default);
    if (nonDefaultBranches.length <= 1) {
      return; // Need at least one non-default branch
    }

    // Remove nodes that belong to this branch
    setNodes((currentNodes) => {
      return (currentNodes as Array<AppNode>).filter((n) => {
        if (isWorkflowBlockNode(n) && n.data.conditionalBranchId === branchId) {
          return false;
        }
        return true;
      });
    });

    // Remove edges that belong to this branch
    setEdges((currentEdges) => {
      return currentEdges.filter((edge) => {
        const edgeData = edge.data as
          | { conditionalBranchId?: string }
          | undefined;
        return edgeData?.conditionalBranchId !== branchId;
      });
    });

    // Remove the branch from the branches array
    const updatedBranches = data.branches.filter((b) => b.id !== branchId);

    // If the deleted branch was active, switch to the first branch
    const newActiveBranchId =
      data.activeBranchId === branchId
        ? (updatedBranches[0]?.id ?? null)
        : data.activeBranchId;

    update({
      branches: updatedBranches,
      activeBranchId: newActiveBranchId,
    });
  };

  const handleMoveBranchUp = (branchId: string) => {
    if (!data.editable) {
      return;
    }

    const nonDefaultBranches = data.branches.filter((b) => !b.is_default);
    const currentIndex = nonDefaultBranches.findIndex((b) => b.id === branchId);
    if (currentIndex <= 0) {
      return; // Already at the top or not found
    }

    // Swap within the non-default array
    const newNonDefaultBranches = [...nonDefaultBranches];
    [
      newNonDefaultBranches[currentIndex],
      newNonDefaultBranches[currentIndex - 1],
    ] = [
      newNonDefaultBranches[currentIndex - 1]!,
      newNonDefaultBranches[currentIndex]!,
    ];

    // Reconstruct the array with default branch(es) at the end.
    const defaultBranches = data.branches.filter((b) => b.is_default);
    const reorderedBranches = [...newNonDefaultBranches, ...defaultBranches];

    update({ branches: reorderedBranches });
  };

  const handleMoveBranchDown = (branchId: string) => {
    if (!data.editable) {
      return;
    }

    const nonDefaultBranches = data.branches.filter((b) => !b.is_default);
    const currentIndex = nonDefaultBranches.findIndex((b) => b.id === branchId);

    if (currentIndex < 0 || currentIndex >= nonDefaultBranches.length - 1) {
      return; // Already at the bottom, not found, or is default branch
    }

    // Swap with the branch below
    const newNonDefaultBranches = [...nonDefaultBranches];
    [
      newNonDefaultBranches[currentIndex],
      newNonDefaultBranches[currentIndex + 1],
    ] = [
      newNonDefaultBranches[currentIndex + 1]!,
      newNonDefaultBranches[currentIndex]!,
    ];

    // Reconstruct with default branch(es) at the end.
    const defaultBranches = data.branches.filter((b) => b.is_default);
    const reorderedBranches = [...newNonDefaultBranches, ...defaultBranches];

    update({ branches: reorderedBranches });
  };

  const handleExpressionChange = (expression: string) => {
    if (!activeBranch || activeBranch.is_default) {
      return;
    }
    update({
      branches: data.branches.map((branch) => {
        if (branch.id !== activeBranch.id) {
          return branch;
        }
        return {
          ...branch,
          criteria: {
            ...(branch.criteria ?? { ...defaultBranchCriteria }),
            expression,
          },
        };
      }),
    });
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 overflow-x-auto">
        <div className="flex items-center gap-2">
          {(() => {
            const MAX_VISIBLE_TABS = 3;
            const totalBranches = orderedBranches.length;
            const nonDefaultBranches = data.branches.filter(
              (b) => !b.is_default,
            );

            // Determine which branches to show in the 3 visible slots
            let visibleBranches: Array<BranchCondition>;
            let overflowBranches: Array<BranchCondition> = [];

            if (totalBranches <= MAX_VISIBLE_TABS) {
              // Show all branches if 3 or fewer
              visibleBranches = orderedBranches;
            } else {
              // Show first 2 + dynamic 3rd slot
              const first2 = orderedBranches.slice(0, 2);
              const activeBranchIndex = orderedBranches.findIndex(
                (b) => b.id === activeBranch?.id,
              );

              if (activeBranchIndex >= 2) {
                // Active branch is 3rd or beyond, show it in 3rd slot
                visibleBranches = [
                  ...first2,
                  orderedBranches[activeBranchIndex]!,
                ];
                // Overflow = all branches except the 3 visible ones
                overflowBranches = orderedBranches.filter(
                  (_, i) => i >= 2 && i !== activeBranchIndex,
                );
              } else {
                // Active branch is in first 2, show 3rd branch normally
                visibleBranches = orderedBranches.slice(0, MAX_VISIBLE_TABS);
                overflowBranches = orderedBranches.slice(MAX_VISIBLE_TABS);
              }
            }

            return (
              <>
                {visibleBranches.map((branch) => {
                  const index = orderedBranches.findIndex(
                    (b) => b.id === branch.id,
                  );
                  const canDelete =
                    data.editable &&
                    !branch.is_default &&
                    nonDefaultBranches.length > 1;

                  const canReorder = !branch.is_default;
                  const branchIndexInNonDefault = nonDefaultBranches.findIndex(
                    (b) => b.id === branch.id,
                  );
                  const canMoveUp = branchIndexInNonDefault > 0;
                  const canMoveDown =
                    branchIndexInNonDefault >= 0 &&
                    branchIndexInNonDefault < nonDefaultBranches.length - 1;

                  const showMenu = canReorder || canDelete;

                  return (
                    <div key={branch.id} className="relative flex">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className={cn(
                          "h-auto rounded-full border p-0 text-xs font-normal transition-colors hover:bg-transparent",
                          showMenu ? "px-3 py-1 pr-7" : "px-3 py-1",
                          {
                            "border-slate-50 bg-slate-50 text-slate-950 hover:bg-slate-50 hover:text-slate-950":
                              branch.id === activeBranch?.id,
                            "border-transparent bg-slate-elevation5 text-tertiary-foreground hover:bg-slate-elevation4 hover:text-tertiary-foreground":
                              branch.id !== activeBranch?.id,
                          },
                        )}
                        onClick={() => handleSelectBranch(branch.id)}
                        disabled={!data.editable}
                      >
                        {getConditionLabel(branch, index)}
                      </Button>
                      {showMenu && (
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className={cn(
                                "absolute right-1 top-1/2 size-auto -translate-y-1/2 rounded-full p-0.5",
                                {
                                  "text-slate-950 hover:bg-slate-300 hover:text-slate-950":
                                    branch.id === activeBranch?.id,
                                  "text-tertiary-foreground hover:bg-accent hover:text-tertiary-foreground dark:hover:bg-slate-600":
                                    branch.id !== activeBranch?.id,
                                },
                              )}
                              onClick={(e) => e.stopPropagation()}
                              title="Branch options"
                            >
                              <DotsVerticalIcon className="size-3" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            {canReorder && (
                              <>
                                <DropdownMenuItem
                                  disabled={!canMoveUp}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    handleMoveBranchUp(branch.id);
                                  }}
                                  className="cursor-pointer"
                                >
                                  Move Up
                                </DropdownMenuItem>
                                <DropdownMenuItem
                                  disabled={!canMoveDown}
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    handleMoveBranchDown(branch.id);
                                  }}
                                  className="cursor-pointer"
                                >
                                  Move Down
                                </DropdownMenuItem>
                              </>
                            )}
                            {canReorder && canDelete && (
                              <DropdownMenuSeparator />
                            )}
                            {canDelete && (
                              <DropdownMenuItem
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleRemoveBranch(branch.id);
                                }}
                                className="cursor-pointer text-red-700 focus:text-red-700 dark:text-red-400 dark:focus:text-red-400"
                              >
                                Remove
                              </DropdownMenuItem>
                            )}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      )}
                    </div>
                  );
                })}

                {overflowBranches.length > 0 && (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-auto gap-1 rounded-full border border-transparent bg-slate-elevation5 p-0 px-3 py-1 text-xs font-normal text-tertiary-foreground transition-colors hover:bg-slate-elevation4 hover:text-tertiary-foreground"
                        disabled={!data.editable}
                      >
                        {overflowBranches.length} More
                        <ChevronDownIcon className="size-3" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start">
                      {overflowBranches.map((branch) => {
                        const index = orderedBranches.findIndex(
                          (b) => b.id === branch.id,
                        );
                        return (
                          <DropdownMenuItem
                            key={branch.id}
                            onClick={() => handleSelectBranch(branch.id)}
                            className="cursor-pointer"
                          >
                            {getConditionLabel(branch, index)}
                          </DropdownMenuItem>
                        );
                      })}
                    </DropdownMenuContent>
                  </DropdownMenu>
                )}

                {/* Add new condition button */}
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={handleAddCondition}
                  disabled={!data.editable}
                  className="size-7 rounded-full border border-transparent bg-slate-elevation5 p-0 text-tertiary-foreground hover:bg-slate-elevation4 hover:text-tertiary-foreground"
                  title="Add new condition"
                >
                  <PlusIcon className="size-4" />
                </Button>
              </>
            );
          })()}
        </div>
      </div>
      {activeBranch && (
        <div className="space-y-2">
          <div className="flex items-center gap-1">
            <Label className="text-xs text-tertiary-foreground">
              {activeBranch.is_default ? "Else branch" : "Expression"}
            </Label>
            {!activeBranch.is_default && (
              <HelpTooltip
                content={`Jinja: {{ y > 100 }}\nNatural language: y is greater than 100\nJinja+Natural language: {{ y }} is greater than 100`}
              />
            )}
          </div>
          <WorkflowBlockInputTextarea
            nodeId={id}
            value={
              activeBranch.is_default
                ? "Executed when no other condition matches"
                : (activeBranch.criteria?.expression ?? "")
            }
            disabled={!data.editable || activeBranch.is_default}
            onChange={(value) => {
              handleExpressionChange(value);
            }}
            placeholder="Enter condition to evaluate (Jinja, natural language, or both)"
            className="nopan text-xs"
          />
        </div>
      )}
    </div>
  );
}

export { BranchesEditor };
