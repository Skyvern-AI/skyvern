import { ReloadIcon } from "@radix-ui/react-icons";

export function RunPlaceholder({ loading }: { loading: boolean }) {
  return (
    <div className="flex h-full w-full items-center justify-center gap-2 p-8 text-center text-sm text-muted-foreground">
      {loading ? (
        <>
          <ReloadIcon className="h-5 w-5 animate-spin" />
          Workflow run is loading…
        </>
      ) : (
        "Run the workflow to watch it live here."
      )}
    </div>
  );
}
