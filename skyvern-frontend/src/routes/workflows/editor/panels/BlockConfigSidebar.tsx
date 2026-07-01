import { Cross2Icon, GearIcon, PlusIcon } from "@radix-ui/react-icons";
import { useNodesData, useReactFlow } from "@xyflow/react";
import { Resizable } from "re-resizable";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  BLOCK_SIDEBAR_WIDTH_MAX,
  BLOCK_SIDEBAR_WIDTH_MIN,
  useBlockSidebarWidthStore,
} from "@/store/BlockSidebarWidthStore";
import { usePendingCommitsStore } from "@/store/PendingCommitsStore";
import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { cn } from "@/util/utils";

import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";

import { useWorkflowEditorMode } from "../hooks/useWorkflowEditorMode";
import { AppNode, isWorkflowBlockNode, WorkflowBlockNode } from "../nodes";
import { EditableNodeTitle } from "../nodes/components/EditableNodeTitle";
import { isStartNode } from "../nodes/StartNode/types";
import { WorkflowBlockIcon } from "../nodes/WorkflowBlockIcon";
import { workflowBlockTitle } from "../nodes/types";
import { WorkflowBlockType } from "../../types/workflowTypes";
import {
  getBlockSidebarGutterPx,
  getContainedBlockSidebarWidth,
} from "../blockSidebar";
import { BlockConfigForm } from "./BlockConfigForm";
import { useHasInteractedThisSession } from "./useHasInteractedThisSession";
import { WorkflowNodeLibraryPanel } from "./WorkflowNodeLibraryPanel";
import type { AddNodeProps } from "../Workspace";

// React Flow node type → backend WorkflowBlockType. The two diverge in
// places (e.g. RF "loop" vs block_type "for_loop") and there is no shared
// translator; the per-node components hardcode their block type when they
// pass it to NodeHeader. Centralizing here keeps that knowledge in one
// place for sidebar consumers — when the dispatcher lands it can reuse
// this map instead of re-deriving it.
type WorkflowBlockNodeType = WorkflowBlockNode["type"];

const NODE_TYPE_TO_BLOCK_TYPE: Record<
  WorkflowBlockNodeType,
  WorkflowBlockType
> = {
  loop: "for_loop",
  conditional: "conditional",
  task: "task",
  textPrompt: "text_prompt",
  sendEmail: "send_email",
  codeBlock: "code",
  fileParser: "file_url_parser",
  upload: "upload_to_s3",
  fileUpload: "file_upload",
  download: "download_to_s3",
  validation: "validation",
  action: "action",
  navigation: "navigation",
  human_interaction: "human_interaction",
  extraction: "extraction",
  login: "login",
  wait: "wait",
  fileDownload: "file_download",
  pdfParser: "pdf_parser",
  taskv2: "task_v2",
  url: "goto_url",
  http_request: "http_request",
  printPage: "print_page",
  workflowTrigger: "workflow_trigger",
  googleSheetsRead: "google_sheets_read",
  googleSheetsWrite: "google_sheets_write",
  pdfFill: "pdf_fill",
};

function getBlockTypeFromNode(node: AppNode): WorkflowBlockType | null {
  if (!isWorkflowBlockNode(node)) {
    return null;
  }
  return NODE_TYPE_TO_BLOCK_TYPE[node.type] ?? null;
}

const FOOTER_TICK_INTERVAL_MS = 10_000;

type SidebarIdentity = {
  label: string;
  isStart: boolean;
  blockType: WorkflowBlockType | null;
};

function getSidebarIdentity(node: AppNode | null): SidebarIdentity {
  const isStart = node ? isStartNode(node) : false;
  const blockType = node ? getBlockTypeFromNode(node) : null;
  const label = isStart
    ? "Agent Settings"
    : typeof node?.data?.label === "string" && node.data.label.length > 0
      ? node.data.label
      : blockType
        ? workflowBlockTitle[blockType]
        : "";
  return { label, isStart, blockType };
}

function SidebarIdentityIcon({
  identity,
}: Readonly<{ identity: SidebarIdentity }>) {
  // The gear doubles as the generic "settings" glyph: it covers both the start
  // node and the unknown-block fallback (a detached/untyped node has no icon).
  return (
    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-slate-600">
      {!identity.isStart && identity.blockType ? (
        <WorkflowBlockIcon
          workflowBlockType={identity.blockType}
          className="size-4"
        />
      ) : (
        <GearIcon className="size-4" />
      )}
    </div>
  );
}

function measureElementWidth(element: HTMLElement): number | null {
  const boundingWidth = element.getBoundingClientRect().width;
  if (boundingWidth > 0) {
    return boundingWidth;
  }

  if (element.offsetWidth > 0) {
    return element.offsetWidth;
  }

  return null;
}

function formatUpdatedAgo(updatedAt: number, now: number): string {
  const elapsedSec = Math.max(0, Math.floor((now - updatedAt) / 1000));
  if (elapsedSec < 60) {
    return `● updated ${elapsedSec} sec ago`;
  }
  const elapsedMin = Math.floor(elapsedSec / 60);
  if (elapsedMin < 60) {
    return `● updated ${elapsedMin} min ago`;
  }
  const elapsedHr = Math.floor(elapsedMin / 60);
  return `● updated ${elapsedHr} hr ago`;
}

function UpdatedAgoFooter({ blockId }: Readonly<{ blockId: string }>) {
  const updatedAt = useSidebarSaveStateStore((state) =>
    state.getLastUpdatedAt(blockId),
  );
  const [now, setNow] = useState<number>(() => Date.now());

  useEffect(() => {
    if (updatedAt === null) {
      return;
    }
    setNow(Date.now());
    const id = setInterval(() => {
      setNow(Date.now());
    }, FOOTER_TICK_INTERVAL_MS);
    return () => {
      clearInterval(id);
    };
  }, [updatedAt]);

  if (updatedAt === null) {
    return null;
  }

  return (
    <footer
      data-testid="block-config-sidebar-updated-footer"
      className="border-t border-border px-4 py-2 text-xs text-slate-400"
    >
      {formatUpdatedAgo(updatedAt, now)}
    </footer>
  );
}

function SubLabel() {
  const hasInteracted = useHasInteractedThisSession();
  if (hasInteracted) return null;
  return (
    <p className="mt-0.5 truncate text-xs text-slate-400">
      Edit settings here · saves automatically
    </p>
  );
}

// Inline-editable block title, mirroring the canvas tile's NodeHeader. Reads
// the live label/editable slice reactively so the heading reflects an edit
// immediately, and routes commits through the same handler the canvas uses
// (sanitize, dedupe, propagate the rename to parameter keys + collapse state).
// Keyed on blockId by the caller so it re-initializes when the selection moves
// to another block without remounting the surrounding sidebar shell. On
// read-only (global) workflows it renders a plain heading: EditableNodeTitle
// still enters its click-to-edit affordance when disabled, which would read
// as misleadingly interactive.
function EditableBlockTitle({
  blockId,
  fallbackLabel,
}: Readonly<{ blockId: string; fallbackLabel: string }>) {
  const data = useNodesData<WorkflowBlockNode>(blockId)?.data;
  const label =
    typeof data?.label === "string" && data.label.length > 0
      ? data.label
      : fallbackLabel;
  const editable = Boolean(data?.editable);
  const [, handleLabelChange] = useNodeLabelChangeHandler({
    id: blockId,
    initialValue: label,
  });

  if (!editable) {
    return (
      <h2 className="truncate text-sm font-medium text-slate-100">{label}</h2>
    );
  }

  return (
    <EditableNodeTitle
      value={label}
      editable
      onChange={handleLabelChange}
      titleClassName="text-sm font-medium text-slate-100"
      inputClassName="text-sm font-medium text-slate-100"
    />
  );
}

function BlockConfigSidebarBody({
  selectedBlockId,
  identity,
  onClose,
}: Readonly<{
  selectedBlockId: string;
  identity: SidebarIdentity;
  onClose: () => void;
}>) {
  return (
    <>
      <header className="flex h-20 items-center justify-between gap-3 border-b border-border px-6">
        <div className="flex min-w-0 items-center gap-3">
          {identity.isStart || identity.blockType ? (
            <SidebarIdentityIcon identity={identity} />
          ) : null}
          <div className="min-w-0">
            {identity.isStart ? (
              <h2 className="truncate text-sm font-medium text-slate-100">
                {identity.label}
              </h2>
            ) : (
              <EditableBlockTitle
                key={selectedBlockId}
                blockId={selectedBlockId}
                fallbackLabel={identity.label}
              />
            )}
            {identity.blockType ? (
              <p
                className="truncate text-xs text-slate-400"
                title={workflowBlockTitle[identity.blockType]}
              >
                {workflowBlockTitle[identity.blockType]}
              </p>
            ) : null}
            <SubLabel />
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close block configuration"
          className="rounded p-1 text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-100"
        >
          <Cross2Icon className="h-4 w-4" />
        </button>
      </header>
      <div className="flex-1 overflow-y-auto px-5 py-4">
        <BlockConfigForm blockId={selectedBlockId} />
      </div>
      <UpdatedAgoFooter blockId={selectedBlockId} />
    </>
  );
}

function BlockLibrarySidebarBody({
  onAddNode,
  onClose,
}: Readonly<{
  onAddNode: (props: AddNodeProps) => void;
  onClose: () => void;
}>) {
  return (
    <>
      <header className="flex h-20 items-center justify-between gap-3 border-b border-border px-6">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-slate-600">
            <PlusIcon className="size-4" />
          </div>
          <div className="min-w-0">
            <h2 className="truncate text-sm font-medium text-slate-100">
              Block Library
            </h2>
            <p className="mt-0.5 truncate text-xs text-slate-400">
              Click on the block type you want to add
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close block library"
          className="rounded p-1 text-slate-400 transition-colors hover:bg-slate-700 hover:text-slate-100"
        >
          <Cross2Icon className="h-4 w-4" />
        </button>
      </header>
      <div className="flex min-h-0 flex-1 overflow-hidden px-5 py-4">
        <WorkflowNodeLibraryPanel onNodeClick={onAddNode} />
      </div>
    </>
  );
}

type BlockConfigSidebarProps = {
  onAddNode?: (props: AddNodeProps) => void;
  // In the studio shell the panel's top/bottom edges align with the Copilot
  // column's py-3 inset rather than the legacy editor's header offsets.
  embedded?: boolean;
};

function BlockConfigSidebar({
  onAddNode,
  embedded = false,
}: BlockConfigSidebarProps) {
  const resizableRef = useRef<Resizable | null>(null);
  const reactFlowInstance = useReactFlow<AppNode>();
  const [editorShellMetrics, setEditorShellMetrics] = useState(() => ({
    gutterPx: getBlockSidebarGutterPx(null),
    width: null as number | null,
  }));
  const width = useBlockSidebarWidthStore((s) => s.width);
  const setWidth = useBlockSidebarWidthStore((s) => s.setWidth);
  const setRenderedWidth = useBlockSidebarWidthStore((s) => s.setRenderedWidth);
  const mode = useWorkflowEditorMode();
  const selectedBlockId = useWorkflowPanelStore(
    (state) => state.selectedBlockId,
  );
  const setSelectedBlockId = useWorkflowPanelStore(
    (state) => state.setSelectedBlockId,
  );
  const workflowPanelState = useWorkflowPanelStore(
    (state) => state.workflowPanelState,
  );
  const closeWorkflowPanel = useWorkflowPanelStore(
    (state) => state.closeWorkflowPanel,
  );
  const flushPendingCommit = usePendingCommitsStore((state) => state.flush);
  const containedWidth = getContainedBlockSidebarWidth(
    width,
    editorShellMetrics.width,
    editorShellMetrics.gutterPx,
  );
  const containedMaxWidth = getContainedBlockSidebarWidth(
    BLOCK_SIDEBAR_WIDTH_MAX,
    editorShellMetrics.width,
    editorShellMetrics.gutterPx,
  );
  const containedMinWidth = Math.min(
    BLOCK_SIDEBAR_WIDTH_MIN,
    containedMaxWidth,
  );

  // Auto-commit on block switch. When `selectedBlockId` flips
  // from A → B, flush any pending commit registered by the dispatcher
  // for block A before the body re-renders for block B. The
  // dispatcher is responsible for actually registering commits; until then
  // this hook is a no-op (`flush` early-returns when no commit is
  // registered for the prior id).
  const previousBlockIdRef = useRef<string | null>(selectedBlockId);
  useEffect(() => {
    const previous = previousBlockIdRef.current;
    if (previous !== null && previous !== selectedBlockId) {
      flushPendingCommit(previous);
    }
    previousBlockIdRef.current = selectedBlockId;
  }, [selectedBlockId, flushPendingCommit]);

  const showLibrary =
    workflowPanelState.active && workflowPanelState.content === "nodeLibrary";
  const sidebarVisible =
    showLibrary || (mode !== "build" && selectedBlockId !== null);

  const selectedNode =
    !showLibrary && selectedBlockId !== null
      ? (reactFlowInstance.getNode(selectedBlockId) ?? null)
      : null;
  const selectedIdentity = getSidebarIdentity(selectedNode);

  useLayoutEffect(() => {
    if (!sidebarVisible) {
      setEditorShellMetrics({
        gutterPx: getBlockSidebarGutterPx(null),
        width: null,
      });
      return;
    }

    const parentElement = resizableRef.current?.resizable?.parentElement;
    if (parentElement === undefined || parentElement === null) {
      setEditorShellMetrics({
        gutterPx: getBlockSidebarGutterPx(null),
        width: null,
      });
      return;
    }

    const updateEditorShellMetrics = () => {
      setEditorShellMetrics({
        gutterPx: getBlockSidebarGutterPx(parentElement),
        width: measureElementWidth(parentElement),
      });
    };

    updateEditorShellMetrics();

    if (typeof ResizeObserver === "undefined") {
      if (typeof window === "undefined") {
        return;
      }

      window.addEventListener("resize", updateEditorShellMetrics);
      return () => {
        window.removeEventListener("resize", updateEditorShellMetrics);
      };
    }

    const resizeObserver = new ResizeObserver(updateEditorShellMetrics);
    resizeObserver.observe(parentElement);

    return () => {
      resizeObserver.disconnect();
    };
  }, [sidebarVisible]);

  useLayoutEffect(() => {
    if (!sidebarVisible) {
      return;
    }

    setRenderedWidth(containedWidth);
  }, [containedWidth, setRenderedWidth, sidebarVisible]);

  // The block-config form only renders when the sidebar shows it directly: build
  // mode gives each block/start node its own inline editor, and the studio shell
  // keeps settings inline in the blocks. In both, the sidebar only hosts the
  // block library (so users can still insert blocks from the canvas).
  if ((mode === "build" || embedded) && !showLibrary) {
    return null;
  }

  if (!showLibrary && selectedBlockId === null) {
    return null;
  }

  return (
    <Resizable
      ref={resizableRef}
      size={{ width: containedWidth, height: "auto" }}
      minWidth={containedMinWidth}
      maxWidth={containedMaxWidth}
      enable={{ left: true }}
      onResizeStop={(_e, _dir, ref) => {
        setWidth(ref.offsetWidth);
      }}
      handleClasses={{ left: "block-sidebar-resize-handle" }}
      handleStyles={{
        left: {
          width: "12px",
          left: "-6px",
          cursor: "col-resize",
        },
      }}
      style={{
        position: "absolute",
        top: mode === "build" ? "7rem" : "2rem",
        right: "1.5rem",
        bottom: "1.5rem",
      }}
      className="z-30"
    >
      <div
        className={cn(
          "relative h-full w-full overflow-hidden",
          "rounded-xl border border-border bg-slate-elevation2 shadow-xl",
        )}
      >
        <aside
          data-testid="block-config-sidebar"
          className="flex h-full w-full flex-col duration-200 ease-out animate-in slide-in-from-right-5"
        >
          {showLibrary ? (
            <BlockLibrarySidebarBody
              onAddNode={(props) => onAddNode?.(props)}
              onClose={closeWorkflowPanel}
            />
          ) : selectedBlockId !== null ? (
            <BlockConfigSidebarBody
              selectedBlockId={selectedBlockId}
              identity={selectedIdentity}
              onClose={() => setSelectedBlockId(null)}
            />
          ) : null}
        </aside>
      </div>
    </Resizable>
  );
}

export { BlockConfigSidebar };
