import { type ComponentProps } from "react";

import { Workspace } from "@/routes/workflows/editor/Workspace";

// PiP shelved for now — no good placement (copilot left, settings right, Pylon corner).
// import { BrowserPiP } from "./BrowserPiP";

export type StudioWorkspaceProps = Omit<
  ComponentProps<typeof Workspace>,
  "showBrowser" | "embedded"
>;

// Editor tab: the real Workspace canvas in embedded mode (the shell owns the top
// bar + Copilot spine, so Workspace's floating header is suppressed).
export function EditorTab(props: StudioWorkspaceProps) {
  return (
    <div className="relative h-full w-full">
      <Workspace {...props} showBrowser={false} embedded />
      {/* <BrowserPiP /> */}
    </div>
  );
}
