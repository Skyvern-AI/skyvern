import { GarbageIcon } from "@/components/icons/GarbageIcon";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  BookmarkFilledIcon,
  BookmarkIcon,
  CopyIcon,
  DotsHorizontalIcon,
  DownloadIcon,
  MixerHorizontalIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useWorkflowRowActions } from "./hooks/useWorkflowRowActions";
import { WorkflowApiResponse } from "./types/workflowTypes";

type Props = {
  workflow: WorkflowApiResponse;
  onSuccessfullyDeleted?: () => void;
  hasParameters?: boolean;
  parametersExpanded?: boolean;
  onToggleParameters?: () => void;
};

function WorkflowActions({
  workflow,
  onSuccessfullyDeleted,
  hasParameters,
  parametersExpanded,
  onToggleParameters,
}: Props) {
  const {
    clone,
    toggleTemplate,
    exportAs,
    deleteWorkflow,
    isDeleting,
    isTogglingTemplate,
  } = useWorkflowRowActions(workflow);

  return (
    <Dialog>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            size="icon"
            variant="ghost"
            className="text-muted-foreground hover:text-foreground"
          >
            <DotsHorizontalIcon className="h-4 w-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent>
          {onToggleParameters ? (
            <>
              <DropdownMenuItem
                onSelect={() => onToggleParameters()}
                disabled={!hasParameters}
                className="p-2"
              >
                <MixerHorizontalIcon className="mr-2 h-4 w-4" />
                {hasParameters
                  ? parametersExpanded
                    ? "Hide parameters"
                    : "Show parameters"
                  : "No parameters"}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
            </>
          ) : null}
          <DropdownMenuItem onSelect={() => clone()} className="p-2">
            <CopyIcon className="mr-2 h-4 w-4" />
            Duplicate Agent
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => toggleTemplate()}
            className="p-2"
            disabled={isTogglingTemplate}
          >
            {workflow.is_template ? (
              <BookmarkFilledIcon className="mr-2 h-4 w-4" />
            ) : (
              <BookmarkIcon className="mr-2 h-4 w-4" />
            )}
            {workflow.is_template
              ? "Remove from Templates"
              : "Save as Template"}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <DownloadIcon className="mr-2 h-4 w-4" />
              Export as...
            </DropdownMenuSubTrigger>
            <DropdownMenuPortal>
              <DropdownMenuSubContent>
                <DropdownMenuItem onSelect={() => exportAs("yaml")}>
                  YAML
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => exportAs("json")}>
                  JSON
                </DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuPortal>
          </DropdownMenuSub>
          <DialogTrigger>
            <DropdownMenuItem className="p-2">
              <GarbageIcon className="mr-2 h-4 w-4 text-destructive" />
              Delete Agent
            </DropdownMenuItem>
          </DialogTrigger>
        </DropdownMenuContent>
      </DropdownMenu>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Are you sure?</DialogTitle>
          <DialogDescription>
            The agent{" "}
            <span className="font-semibold text-primary">{workflow.title}</span>{" "}
            will be deleted.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => deleteWorkflow({ onSuccess: onSuccessfullyDeleted })}
            disabled={isDeleting}
          >
            {isDeleting && <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { WorkflowActions };
