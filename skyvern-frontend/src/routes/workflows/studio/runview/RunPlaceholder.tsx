import { ReloadIcon } from "@radix-ui/react-icons";

export function RunPlaceholder({
  loading,
  unavailable = false,
}: {
  loading: boolean;
  unavailable?: boolean;
}) {
  // A run whose status request failed is not a run that is still arriving, and
  // not a run that produced nothing — saying "loading" for it never resolves.
  if (unavailable) {
    return (
      <div className="flex h-full w-full items-center justify-center gap-2 p-8 text-center text-sm text-destructive">
        This run could not be loaded.
      </div>
    );
  }
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
