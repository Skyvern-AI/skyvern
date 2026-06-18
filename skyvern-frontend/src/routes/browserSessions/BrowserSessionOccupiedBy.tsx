import { Link } from "react-router-dom";

import { CopyText } from "@/routes/workflows/editor/Workspace";

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
      <CopyText className="opacity-75 hover:opacity-100" text={runnableId} />
    </div>
  );
}

export { BrowserSessionOccupiedBy };
