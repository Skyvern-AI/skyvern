import { Link } from "react-router-dom";

import { CopyButton } from "@/components/CopyButton";

function BrowserSessionOccupiedBy({ runnableId }: { runnableId: string }) {
  return (
    <div className="flex items-center justify-end gap-1">
      <span className="text-xs opacity-75">In use by</span>
      <Link
        to={`/runs/${runnableId}`}
        className="max-w-[20rem] truncate font-mono text-xs text-primary underline-offset-4 hover:underline"
      >
        {runnableId}
      </Link>
      <CopyButton
        className="opacity-75 hover:opacity-100"
        value={runnableId}
        ariaLabel="Copy run ID"
      />
    </div>
  );
}

export { BrowserSessionOccupiedBy };
