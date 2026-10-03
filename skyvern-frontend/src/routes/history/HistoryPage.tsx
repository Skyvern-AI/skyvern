import { CreatorDirectoryBoundary } from "@/components/CreatorDirectoryBoundary";

import { RunHistory } from "./RunHistory";

function HistoryPage() {
  return (
    <CreatorDirectoryBoundary>
      <div className="space-y-6">
        <RunHistory />
      </div>
    </CreatorDirectoryBoundary>
  );
}

export { HistoryPage };
