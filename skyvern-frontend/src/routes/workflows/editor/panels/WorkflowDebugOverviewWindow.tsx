/**
 * NOTE(jdo): this is a hack: we are iframe-ing the overview page, but we really
 * need dedicated UI component for this.
 */

import { FloatingWindow } from "@/components/FloatingWindow";
import { useMemo, useRef } from "react";
import { useParams } from "react-router-dom";

function WorkflowDebugOverviewWindow() {
  return (
    <FloatingWindow
      title="Live View"
      initialWidth={256}
      initialHeight={512}
      maximized={true}
      showMaximizeButton={true}
    >
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
    // waving hand emoji

    <div className="flex h-full w-full flex-col items-center justify-center gap-4 overflow-y-auto rounded-xl bg-[#020817] p-6">
      <div className="flex h-full w-full max-w-[15rem] flex-col items-center justify-center gap-4 rounded-xl bg-[#020817] p-6">
        <div>
          Hi! 👋 We're experimenting with a new feature called debugger.
        </div>
        <div>
          This debugger allows you to see the state of your workflow in a live
          browser.
        </div>
        <div>You can run individual blocks, instead of the whole workflow.</div>
        <div>
          To get started, press the play button on a block in your workflow.
        </div>
      </div>
    </div>
  );
}

export { WorkflowDebugOverviewWindow };
