/**
 * NOTE(jdo): this is not a "panel", in the react-flow sense. It's a floating,
 * draggable, resizeable window, like on a desktop. But I am putting it here
 * for now.
 */

import { Resizable } from "re-resizable";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import Draggable from "react-draggable";
import { useParams } from "react-router-dom";

import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";

/**
 * TODO(jdo): extract this to a reusable Window component.
 */
function WorkflowDebugOverviewWindow() {
  const debugStore = useDebugStore();
  const isDebugMode = debugStore.isDebugMode;
  const [position, setPosition] = useState({ x: 0, y: 0 });
  const [size, setSize] = useState({
    left: 0,
    top: 0,
    width: 800,
    height: 680,
  });
  const [lastSize, setLastSize] = useState({
    left: 0,
    top: 0,
    width: 800,
    height: 680,
  });
  const [sizeBeforeMaximize, setSizeBeforeMaximize] = useState({
    left: 0,
    top: 0,
    width: 800,
    height: 680,
  });
  const [isMaximized, setIsMaximized] = useState(false);
  const parentRef = useRef<HTMLDivElement>(null);
  const resizableRef = useRef<HTMLDivElement>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isResizing, setIsResizing] = useState(false);
  const [dragStartSize, setDragStartSize] = useState<
    | {
        left: number;
        top: number;
        width: number;
        height: number;
      }
    | undefined
  >(undefined);

  const onResize = useCallback(
    ({
      delta,
      direction,
      size,
    }: {
      delta: { width: number; height: number };
      direction: string;
      size: { left: number; top: number; width: number; height: number };
    }) => {
      if (!dragStartSize) {
        return;
      }

      const top =
        resizableRef.current?.parentElement?.offsetTop ?? lastSize.top;
      const left =
        resizableRef.current?.parentElement?.offsetLeft ?? lastSize.left;
      const width =
        resizableRef.current?.parentElement?.offsetWidth ?? lastSize.width;
      const height =
        resizableRef.current?.parentElement?.offsetHeight ?? lastSize.height;

      setLastSize({ top, left, width, height });
      const directions = ["top", "left", "topLeft", "bottomLeft", "topRight"];

      if (directions.indexOf(direction) !== -1) {
        let newLeft = size.left;
        let newTop = size.top;

        if (direction === "bottomLeft") {
          newLeft = dragStartSize.left - delta.width;
        } else if (direction === "topRight") {
          newTop = dragStartSize.top - delta.height;
        } else {
          newLeft = dragStartSize.left - delta.width;
          newTop = dragStartSize.top - delta.height;
        }

        // TODO(follow-up): https://github.com/bokuweb/re-resizable/issues/868
        flushSync(() => {
          setSize({
            ...size,
            left: newLeft,
            top: newTop,
          });
          setPosition({ x: newLeft, y: newTop });
        });
      } else {
        flushSync(() => {
          setSize({
            ...size,
            left: size.left,
            top: size.top,
          });
          setPosition({ x: size.left, y: size.top });
        });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dragStartSize],
  );

  /**
   * Forces the sizing to take place after the resize is complete.
   *
   * TODO(jdo): emits warnings in the dev console. ref: https://github.com/bokuweb/re-resizable/issues/868
   */
  useEffect(() => {
    if (isResizing) {
      return;
    }
    const width = lastSize.width;
    const height = lastSize.height;

    flushSync(() => {
      setSize({
        ...size,
        width,
        height,
      });
    });

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isResizing]);

  const onDrag = (position: { x: number; y: number }) => {
    if (isMaximized) {
      restore();
      return;
    }

    setPosition({ x: position.x, y: position.y });

    setSize({
      ...size,
      left: position.x,
      top: position.y,
    });

    setLastSize({
      ...size,
      left: position.x,
      top: position.y,
    });
  };

  const onDblClickHeader = () => {
    if (!isMaximized) {
      maximize();
    } else {
      restore();
    }
  };

  const maximize = () => {
    const parent = parentRef.current;

    if (!parent) {
      console.warn("No parent - cannot maximize.");
      return;
    }

    setSizeBeforeMaximize({
      ...size,
      left: position.x,
      top: position.y,
    });

    setIsMaximized(true);

    setSize({
      left: 0,
      top: 0,
      // has to take into account padding...hack
      width: parent.offsetWidth - 16,
      height: parent.offsetHeight - 16,
    });

    setPosition({ x: 0, y: 0 });
  };

  const restore = () => {
    const restoreSize = sizeBeforeMaximize;

    const position = isDragging
      ? { left: 0, top: 0 }
      : {
          left: restoreSize.left,
          top: restoreSize.top,
        };

    setSize({
      left: position.left,
      top: position.top,
      width: restoreSize.width,
      height: restoreSize.height,
    });

    setPosition({ x: position.left, y: position.top });

    setIsMaximized(false);
  };

  /**
   * If maximized, need to retain max size during parent resizing.
   */
  useLayoutEffect(() => {
    const observer = new ResizeObserver(() => {
      const parent = parentRef.current;

      if (!parent) {
        return;
      }

      if (isMaximized) {
        setSize({
          left: 0,
          top: 0,
          // has to take into account padding...hack
          width: parent.offsetWidth - 16,
          height: parent.offsetHeight - 16,
        });
      }
    });

    observer.observe(parentRef.current!);

    return () => {
      observer.disconnect();
    };
  }, [isMaximized]);

  return !isDebugMode ? null : (
    <div
      ref={parentRef}
      style={{
        position: "absolute",
        background: "transparent",
        top: 0,
        left: 0,
        width: "100%",
        height: "100%",
        pointerEvents: "none",
        padding: "0.5rem",
      }}
    >
      <Draggable
        handle=".my-panel-header"
        position={position}
        onStart={() => setIsDragging(true)}
        onDrag={(_, data) => onDrag(data)}
        onStop={() => setIsDragging(false)}
        bounds="parent"
        disabled={isResizing}
      >
        <Resizable
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: "#020817",
            boxSizing: "border-box",
            pointerEvents: "auto",
          }}
          className={cn("border-8 border-gray-900", {
            "hover:border-slate-500": !isMaximized,
          })}
          bounds={parentRef.current ?? "parent"}
          enable={
            isMaximized
              ? false
              : {
                  top: true,
                  right: true,
                  bottom: true,
                  left: true,
                  topRight: true,
                  bottomRight: true,
                  bottomLeft: true,
                  topLeft: true,
                }
          }
          onResizeStart={() => {
            if (isMaximized) {
              return;
            }

            setIsResizing(true);
            setDragStartSize({ ...size, left: position.x, top: position.y });
          }}
          onResize={(_, direction, __, delta) => {
            if (isMaximized) {
              return;
            }

            onResize({ delta, direction, size });
          }}
          onResizeStop={() => {
            if (isMaximized) {
              return;
            }

            setIsResizing(false);
            setDragStartSize(undefined);
          }}
          defaultSize={size}
          size={size}
        >
          <div
            ref={resizableRef}
            className="my-panel"
            style={{
              pointerEvents: "auto",
              padding: "0px",
              width: "100%",
              height: "100%",
              display: "flex",
              flexDirection: "column",
            }}
            onDoubleClick={() => {
              onDblClickHeader();
            }}
          >
            <div className="my-panel-header w-full cursor-move bg-[#031827] p-3">
              Live View
            </div>
            <WorkflowDebugOverviewWindowIframe />
          </div>
        </Resizable>
      </Draggable>
    </div>
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
