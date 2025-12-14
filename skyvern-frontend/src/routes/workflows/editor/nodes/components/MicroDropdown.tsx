import { ChevronDownIcon, ChevronUpIcon } from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";

import { cn } from "@/util/utils";

interface Props {
  selections: string[];
  selected: string;
  // --
  onChange: (selection: string) => void;
}

function MicroDropdown({ selections, selected, onChange }: Props) {
  const dropdownRef = useRef<HTMLDivElement>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [openUpwards, setOpenUpwards] = useState(false);

  function handleOnChange(selection: string) {
    setIsOpen(false);
    onChange(selection);
  }

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    function handleClickOutside(event: MouseEvent) {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    }

    document.addEventListener("mousedown", handleClickOutside, true);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside, true);
    };
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    function handleEscKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setIsOpen(false);
      }
    }

    document.addEventListener("keydown", handleEscKey);
    return () => {
      document.removeEventListener("keydown", handleEscKey);
    };
  }, [isOpen]);

  return (
    <div ref={dropdownRef} className="relative inline-block">
      <div className="flex items-center gap-1">
        <div
          className="relative inline-flex p-0 text-xs text-[#00d2ff]"
          onClick={() => {
            if (!isOpen && dropdownRef.current) {
              const rect = dropdownRef.current.getBoundingClientRect();
              const viewportHeight = window.innerHeight;
              const componentMiddle = rect.top + rect.height / 2;
              setOpenUpwards(componentMiddle > viewportHeight * 0.5);
            }
            setIsOpen(!isOpen);
          }}
        >
          [{selected}]
          {isOpen ? (
            <ChevronUpIcon className="ml-1 h-4 w-4" />
          ) : (
            <ChevronDownIcon className="ml-1 h-4 w-4" />
          )}
          {isOpen && (
            <div
              className={cn(
                "absolute right-0 z-10 rounded-md bg-background text-xs text-slate-400",
                "duration-200 animate-in fade-in-0 zoom-in-95",
                openUpwards
                  ? "bottom-full mb-2 slide-in-from-bottom-2"
                  : "top-full mt-2 slide-in-from-top-2",
              )}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="space-y-1 p-2">
                {selections.map((s, index) => (
                  <div
                    key={s}
                    className={cn(
                      "flex cursor-pointer items-center gap-1 rounded-md p-1 hover:bg-slate-800",
                      "animate-in fade-in-0 slide-in-from-left-2",
                      {
                        "pointer-events-none cursor-default opacity-50":
                          s === selected,
                      },
                    )}
                    style={{
                      animationDelay: `${(index + 1) * 80}ms`,
                      animationDuration: "200ms",
                      animationFillMode: "backwards",
                    }}
                  >
                    <div
                      onClick={() => {
                        if (s === selected) {
                          return;
                        }

                        handleOnChange(s);
                      }}
                    >
                      {s === selected ? `[${s}]` : s}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export { MicroDropdown };
