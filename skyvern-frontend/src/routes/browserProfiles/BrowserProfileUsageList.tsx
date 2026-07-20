import { BrowserProfileUsage } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";

type Props = {
  usage: BrowserProfileUsage | undefined;
  isLoading: boolean;
};

function plural(count: number, noun: string) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function BrowserProfileUsageList({ usage, isLoading }: Props) {
  if (isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-4 w-56" />
      </div>
    );
  }

  if (!usage) {
    return null;
  }

  const { workflows, credentials, recent_seeded_run_count } = usage;
  const nothingUsesIt =
    workflows.length === 0 &&
    credentials.length === 0 &&
    recent_seeded_run_count === 0;

  if (nothingUsesIt) {
    return (
      <p className="text-sm text-muted-foreground">
        Nothing else uses this browser profile yet.
      </p>
    );
  }

  return (
    <div className="space-y-3 text-sm">
      {credentials.length > 0 && (
        <div className="space-y-1">
          <p className="font-medium">
            Linked to {plural(credentials.length, "credential")}
          </p>
          <ul className="list-disc space-y-0.5 pl-5 text-muted-foreground">
            {credentials.map((credential) => (
              <li key={credential.credential_id} className="truncate">
                {credential.name}
              </li>
            ))}
          </ul>
        </div>
      )}
      {workflows.length > 0 && (
        <div className="space-y-1">
          <p className="font-medium">
            Used by {plural(workflows.length, "workflow")}
          </p>
          <ul className="list-disc space-y-0.5 pl-5 text-muted-foreground">
            {workflows.map((workflow) => (
              <li key={workflow.workflow_permanent_id} className="truncate">
                {workflow.title}
              </li>
            ))}
          </ul>
        </div>
      )}
      {recent_seeded_run_count > 0 && (
        <p className="text-muted-foreground">
          {plural(recent_seeded_run_count, "recent run")} used this profile.
        </p>
      )}
    </div>
  );
}

export { BrowserProfileUsageList };
