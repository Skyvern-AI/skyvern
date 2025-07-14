/**
 * A draggable, resizable, floating window.
 *
 * NOTE: there is copious use of flushSync; see TODOs. We will need to remove
 * this. (We can build our own windowing from scratch, sans `react-draggable`
 * and `re-resizable`; but I don't want to do that until it's worth the effort.)
 */

import { Resizable } from "re-resizable";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import Draggable from "react-draggable";

import { cn } from "@/util/utils";

type OS = "Windows" | "macOS" | "Linux" | "Unknown";

function MacOsButton(props: {
  color: string;
  children?: React.ReactNode;
  disabled?: boolean;
  tip?: string;
  // --
  onClick: () => void;
}) {
  return (
    <button
      disabled={props.disabled}
      onClick={props.disabled ? undefined : props.onClick}
      className="group flex h-[0.8rem] w-[0.8rem] items-center justify-center rounded-full text-black opacity-50 hover:opacity-100"
      style={{ backgroundColor: props.disabled ? "#444" : props.color }}
      title={props.tip}
    >
      <div className="hidden h-full w-full items-center justify-center group-hover:flex">
        {props.children ?? null}
      </div>
    </button>
  );
}

function WindowsButton(props: {
  children?: React.ReactNode;
  disabled?: boolean;
  tip?: string;
  // --
  onClick: () => void;
}) {
  return (
    <button
      disabled={props.disabled}
      onClick={props.disabled ? undefined : props.onClick}
      className="flex h-[0.8rem] w-[0.8rem] items-center justify-center gap-2 text-white opacity-80 hover:opacity-100"
      style={{ opacity: props.disabled ? 0.5 : 1 }}
      title={props.tip}
    >
      {props.children ?? null}
    </button>
  );
}

function getOs(): OS {
  if (typeof navigator === "undefined") {
    return "Unknown"; // For non-browser environments
  }

  const platform = navigator.platform.toLowerCase();
  const userAgent = navigator.userAgent.toLowerCase();

  if (platform.includes("win") || userAgent.includes("windows")) {
    return "Windows";
  }

  if (platform.includes("mac") || userAgent.includes("mac os")) {
    return "macOS";
  }

  if (
    platform.includes("linux") ||
    userAgent.includes("linux") ||
    userAgent.includes("x11")
  ) {
    return "Linux";
  }

  return "Unknown";
}

function FloatingWindow({
  children,
  title,
}: {
  children: React.ReactNode;
  title: string;
}) {
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
  const [isMinimized, setIsMinimized] = useState(false);
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

  const os = getOs();

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

  const toggleMaximized = () => {
    if (!isMaximized) {
      maximize();
    } else {
      restore();
    }

    setIsMinimized(false);
  };

  const onMinimize = () => {
    setIsMinimized(true);
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

    parentRef.current && observer.observe(parentRef.current);

    return () => {
      observer.disconnect();
    };
  }, [isMaximized]);

  return (
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
        handle=".my-window-header"
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
            className="my-window"
            style={{
              pointerEvents: "auto",
              padding: "0px",
              width: "100%",
              height: "100%",
              display: "flex",
              flexDirection: "column",
            }}
            onDoubleClick={() => {
              toggleMaximized();
            }}
          >
            <div className="my-window-header flex w-full cursor-move items-center justify-start gap-2 bg-[#031827] p-3">
              {os === "macOS" ? (
                <>
                  <div className="buttons-container flex items-center gap-2">
                    <MacOsButton
                      color="#FF605C"
                      disabled={true}
                      tip="close"
                      onClick={() => {}}
                    />
                    <MacOsButton
                      color="#FFBD44"
                      disabled={true}
                      tip="minimize"
                      onClick={onMinimize}
                    />
                    <MacOsButton
                      color="#00CA4E"
                      tip={isMaximized || isMinimized ? "restore" : "maximize"}
                      onClick={toggleMaximized}
                    />
                  </div>
                  <div className="ml-auto">{title}</div>
                </>
              ) : (
                <>
                  <div>{title}</div>
                  <div className="buttons-container ml-auto flex h-full items-center gap-4">
                    <WindowsButton onClick={toggleMaximized}>
                      <div>{isMaximized ? "-" : "□"}</div>
                    </WindowsButton>
                  </div>
                </>
              )}
            </div>
            {children}
          </div>
        </Resizable>
      </Draggable>
    </div>
  );
}

export { FloatingWindow };
