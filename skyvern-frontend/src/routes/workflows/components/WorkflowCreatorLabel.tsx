import { Skeleton } from "@/components/ui/skeleton";
import { useWorkflowCreatorDirectory } from "@/store/WorkflowCreatorContext";

type Props = {
  createdBy: string | null | undefined;
};

function WorkflowCreatorLabel({ createdBy }: Props) {
  const { resolveMemberName, isSettled } = useWorkflowCreatorDirectory();
  // Non-member values: null for API-key creates and pre-attribution rows, "{org_id}_user" for the shared UI key.
  const name =
    createdBy === "copilot"
      ? "Copilot"
      : createdBy
        ? resolveMemberName(createdBy)
        : null;

  if (!name) {
    // An id the directory has not finished loading is unknown, not unattributed.
    if (createdBy && !isSettled) {
      return <Skeleton className="h-5 w-20" />;
    }
    return <span className="text-muted-foreground">-</span>;
  }
  return (
    <span className="block truncate text-muted-foreground" title={name}>
      {name}
    </span>
  );
}

export { WorkflowCreatorLabel };
