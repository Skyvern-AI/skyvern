import { useState } from "react";
import { ParametersState } from "../types";
import { WorkflowParameterEditPanel } from "./WorkflowParameterEditPanel";
import { MixerVerticalIcon, PlusIcon } from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import { GarbageIcon } from "@/components/icons/GarbageIcon";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { DialogClose } from "@radix-ui/react-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useReactFlow } from "@xyflow/react";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import {
  WorkflowEditorParameterType,
  WorkflowEditorParameterTypes,
} from "../../types/workflowTypes";
import { getLabelForWorkflowParameterType } from "../workflowEditorUtils";

const WORKFLOW_EDIT_PANEL_WIDTH = 20 * 16;
const WORKFLOW_EDIT_PANEL_GAP = 1 * 16;

interface Props {
  onMouseDownCapture?: () => void;
}

function WorkflowParametersPanel({ onMouseDownCapture }: Props) {
  const setHasChanges = useWorkflowHasChangesStore(
    (state) => state.setHasChanges,
  );
  const {
    parameters: workflowParameters,
    setParameters: setWorkflowParameters,
  } = useWorkflowParametersStore();
  const [operationPanelState, setOperationPanelState] = useState<{
    active: boolean;
    operation: "add" | "edit";
    parameter?: ParametersState[number] | null;
    type: WorkflowEditorParameterType;
  }>({
    active: false,
    operation: "add",
    parameter: null,
    type: "workflow",
  });
  const { setNodes } = useReactFlow();

  return (
    <div
      className="relative z-10 w-[25rem] rounded-xl border border-slate-700 bg-slate-950 p-5 shadow-xl"
      onMouseDownCapture={() => onMouseDownCapture?.()}
    >
      <div className="space-y-4">
        <header>
          <h1 className="text-lg">Parameters</h1>
          <span className="text-sm text-slate-400">
            Create placeholder values that you can link in nodes. You will be
            prompted to fill them in before running your workflow.
          </span>
        </header>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button className="w-full">
              <PlusIcon className="mr-2 h-6 w-6" />
              Add Parameter
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent className="w-60">
            <DropdownMenuLabel>Add Parameter</DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onClick={() => {
                setOperationPanelState({
                  active: true,
                  operation: "add",
                  type: WorkflowEditorParameterTypes.Workflow,
                });
              }}
            >
              Input Parameter
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={() => {
                setOperationPanelState({
                  active: true,
                  operation: "add",
                  type: WorkflowEditorParameterTypes.Credential,
                });
              }}
            >
              Credential Parameter
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={() => {
                setOperationPanelState({
                  active: true,
                  operation: "add",
                  type: WorkflowEditorParameterTypes.Secret,
                });
              }}
            >
              Secret Parameter
            </DropdownMenuItem>
            <DropdownMenuItem
              onClick={() => {
                setOperationPanelState({
                  active: true,
                  operation: "add",
                  type: WorkflowEditorParameterTypes.CreditCardData,
                });
              }}
            >
              Credit Card Parameter
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <ScrollArea>
          <ScrollAreaViewport className="max-h-96">
            <section className="space-y-2">
              {workflowParameters.map((parameter) => {
                return (
                  <div
                    key={parameter.key}
                    className="flex items-center justify-between gap-2 rounded-md bg-slate-elevation1 px-3 py-2"
                  >
                    <div className="flex min-w-0 items-center gap-4">
                      <span className="truncate text-sm" title={parameter.key}>
                        {parameter.key}
                      </span>
                      {parameter.parameterType === "workflow" ? (
                        <span className="text-sm text-slate-400">
                          {getLabelForWorkflowParameterType(parameter.dataType)}
                        </span>
                      ) : (
                        <span className="text-sm text-slate-400">
                          {parameter.parameterType === "onepassword"
                            ? "credential"
                            : parameter.parameterType}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <MixerVerticalIcon
                        className="cursor-pointer"
                        onClick={() => {
                          setOperationPanelState({
                            active: true,
                            operation: "edit",
                            parameter: parameter,
                            type:
                              parameter.parameterType === "onepassword"
                                ? WorkflowEditorParameterTypes.Credential
                                : parameter.parameterType,
                          });
                        }}
                      />
                      <Dialog>
                        <DialogTrigger>
                          <GarbageIcon className="size-4 cursor-pointer text-destructive-foreground text-red-600" />
                        </DialogTrigger>
                        <DialogContent>
                          <DialogHeader>
                            <DialogTitle>Are you sure?</DialogTitle>
                            <DialogDescription>
                              This parameter will be deleted.
                            </DialogDescription>
                          </DialogHeader>
                          <DialogFooter>
                            <DialogClose asChild>
                              <Button variant="secondary">Cancel</Button>
                            </DialogClose>
                            <Button
                              variant="destructive"
                              onClick={() => {
                                setWorkflowParameters(
                                  workflowParameters.filter(
                                    (p) => p.key !== parameter.key,
                                  ),
                                );
                                setHasChanges(true);
                                setNodes((nodes) => {
                                  return nodes.map((node) => {
                                    if (
                                      node.type === "task" ||
                                      node.type === "textPrompt"
                                    ) {
                                      return {
                                        ...node,
                                        data: {
                                          ...node.data,
                                          parameterKeys: (
                                            node.data
                                              .parameterKeys as Array<string>
                                          ).filter(
                                            (key) => key !== parameter.key,
                                          ),
                                        },
                                      };
                                    }
                                    return node;
                                  });
                                });
                              }}
                            >
                              Delete
                            </Button>
                          </DialogFooter>
                        </DialogContent>
                      </Dialog>
                    </div>
                  </div>
                );
              })}
            </section>
          </ScrollAreaViewport>
        </ScrollArea>
      </div>
      {operationPanelState.active && (
        <div
          className="absolute"
          style={{
            top: 0,
            left: -1 * (WORKFLOW_EDIT_PANEL_WIDTH + WORKFLOW_EDIT_PANEL_GAP),
          }}
        >
          {operationPanelState.operation === "add" && (
            <div className="w-80 rounded-xl border border-slate-700 bg-slate-950 p-5 px-2 shadow-xl">
              <WorkflowParameterEditPanel
                type={operationPanelState.type}
                onSave={(parameter) => {
                  setWorkflowParameters([...workflowParameters, parameter]);
                  setHasChanges(true);
                  setOperationPanelState({
                    active: false,
                    operation: "add",
                    type: "workflow",
                  });
                }}
                onClose={() => {
                  setOperationPanelState({
                    active: false,
                    operation: "add",
                    type: "workflow",
                  });
                }}
              />
            </div>
          )}
          {operationPanelState.operation === "edit" &&
            operationPanelState.parameter && (
              <div className="w-80 rounded-xl border border-slate-700 bg-slate-950 p-5 px-2 shadow-xl">
                <WorkflowParameterEditPanel
                  key={operationPanelState.parameter?.key}
                  type={operationPanelState.type}
                  initialValues={operationPanelState.parameter}
                  onSave={(editedParameter) => {
                    setHasChanges(true);
                    setWorkflowParameters(
                      workflowParameters.map((parameter) => {
                        if (
                          parameter.key === operationPanelState.parameter?.key
                        ) {
                          return editedParameter;
                        }
                        if (
                          parameter.parameterType === "context" &&
                          parameter.sourceParameterKey ===
                            operationPanelState.parameter?.key
                        ) {
                          return {
                            ...parameter,
                            sourceParameterKey: editedParameter.key,
                          };
                        }
                        return parameter;
                      }),
                    );
                    setNodes((nodes) => {
                      return nodes.map((node) => {
                        if (
                          node.type === "task" ||
                          node.type === "textPrompt"
                        ) {
                          return {
                            ...node,
                            data: {
                              ...node.data,
                              parameterKeys: (
                                node.data.parameterKeys as Array<string>
                              ).map((key) => {
                                if (
                                  key === operationPanelState.parameter?.key
                                ) {
                                  return editedParameter.key;
                                }
                                return key;
                              }),
                            },
                          };
                        }
                        return node;
                      });
                    });
                    setOperationPanelState({
                      active: false,
                      operation: "edit",
                      parameter: null,
                      type: "workflow",
                    });
                  }}
                  onClose={() => {
                    setOperationPanelState({
                      active: false,
                      operation: "edit",
                      parameter: null,
                      type: "workflow",
                    });
                  }}
                />
              </div>
            )}
        </div>
      )}
    </div>
  );
}

export { WorkflowParametersPanel };
