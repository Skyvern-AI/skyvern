/**
 * NOTE(jdo): this is a hack: we are iframe-ing the overview page, but we really
 * need dedicated UI component for this.
 */

import { FloatingWindow } from "@/components/FloatingWindow";
import { useMemo, useRef } from "react";
import { useParams } from "react-router-dom";

function WorkflowDebugOverviewWindow() {
  return (
    <FloatingWindow title="Live View">
      <WorkflowDebugOverviewWindowIframe />
    </FloatingWindow>
  );
}

function WorkflowDebugOverviewWindowIframe() {
  const { workflowPermanentId: wpid, workflowRunId: wrid } = useParams();
  const lastCompletePair = useRef<{ wpid: string; wrid: string } | null>(null);

  if (wpid !== undefined && wrid !== undefined) {
    lastCompletePair.current = {
      wpid,
      wrid,
    };
  }

  const paramsToUse = useMemo(() => {
    if (wpid && wrid) {
      return { wpid, wrid };
    }
    return lastCompletePair.current;
  }, [wpid, wrid]);

  const origin = location.origin;
  const dest = paramsToUse
    ? `${origin}/workflows/${paramsToUse.wpid}/${paramsToUse.wrid}/overview?embed=true`
    : null;

  return dest ? (
    <div className="h-full w-full rounded-xl bg-[#020817] p-6">
      <iframe src={dest} className="h-full w-full rounded-xl" />
    </div>
  ) : (
    <div className="h-full w-full rounded-xl bg-[#020817] p-6">
      <p>Workflow not found</p>
    </div>
  );
}

export { WorkflowDebugOverviewWindow };
