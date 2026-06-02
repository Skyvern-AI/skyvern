import { Link, useLocation } from "react-router-dom";
import { ChevronDownIcon } from "@radix-ui/react-icons";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

type SidebarNavChild = {
  label: string;
  to?: string;
  icon?: ReactNode;
  external?: boolean;
  onClick?: () => void;
  active?: boolean;
  disabled?: boolean;
};

type SidebarNavItem = {
  label: string;
  to: string;
  icon: ReactNode;
  badge?: string;
  defaultOpen?: boolean | (() => boolean);
  initialVisibleChildren?: number;
  children?: Array<SidebarNavChild>;
};

type Props = {
  items: Array<SidebarNavItem>;
  collapsed?: boolean;
};

const OPEN_GROUPS_STORAGE_KEY = "skyvern-sidebar-open-groups";
const EXPANDED_CHILDREN_STORAGE_KEY = "skyvern-sidebar-expanded-children";

function readStoredRecord(key: string) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    return parsed as Record<string, boolean>;
  } catch {
    return {};
  }
}

function writeStoredRecord(key: string, value: Record<string, boolean>) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // localStorage can fail in private or embedded contexts; collapse state is
    // cosmetic, so keep the in-memory state and skip persistence.
  }
}

function getItemStorageKey(item: SidebarNavItem) {
  return item.to;
}

function normalizePath(to: string) {
  return to.split("?")[0] || "/";
}

function resolveDefaultOpen(item: SidebarNavItem) {
  if (typeof item.defaultOpen === "function") {
    return item.defaultOpen();
  }
  return item.defaultOpen !== false;
}

function queryMatches(to: string, search: string, requireExactQuery = false) {
  const query = to.split("?")[1];
  if (!query) {
    return !requireExactQuery || search === "";
  }

  try {
    const targetParams = new URLSearchParams(query);
    const currentParams = new URLSearchParams(search);
    const targetEntries = Array.from(targetParams.entries());

    if (
      requireExactQuery &&
      targetEntries.length !== Array.from(currentParams.entries()).length
    ) {
      return false;
    }

    for (const [key, value] of targetEntries) {
      if (!currentParams.getAll(key).includes(value)) {
        return false;
      }
    }
    return true;
  } catch {
    return false;
  }
}

function useIsActive() {
  const location = useLocation();

  return useCallback(
    (to: string, exact = false, requireExactQuery = false) => {
      const path = normalizePath(to);
      const pathMatches = exact
        ? location.pathname === path
        : location.pathname === path ||
          location.pathname.startsWith(`${path}/`);

      return (
        pathMatches && queryMatches(to, location.search, requireExactQuery)
      );
    },
    [location.pathname, location.search],
  );
}

// Open on hover only after a brief rest, so a cursor crossing the icon rail doesn't flash menus open.
const FLYOUT_OPEN_DELAY_MS = 100;
// Keep the flyout open briefly after the cursor leaves, so it can bridge the gap to the content.
const FLYOUT_CLOSE_DELAY_MS = 120;

function CollapsedNavItem({
  item,
  groupStorageKey,
  triggerClassName,
  iconClassName,
  menuChildItemClassName,
}: {
  item: SidebarNavItem;
  groupStorageKey: string;
  triggerClassName: string;
  iconClassName: string;
  menuChildItemClassName: string;
}) {
  const [open, setOpen] = useState(false);
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearHoverTimer = useCallback(() => {
    if (hoverTimerRef.current !== null) {
      clearTimeout(hoverTimerRef.current);
      hoverTimerRef.current = null;
    }
  }, []);

  const scheduleHover = useCallback(
    (nextOpen: boolean, delay: number) => {
      clearHoverTimer();
      hoverTimerRef.current = setTimeout(() => {
        hoverTimerRef.current = null;
        setOpen(nextOpen);
      }, delay);
    },
    [clearHoverTimer],
  );

  useEffect(() => {
    return () => {
      clearHoverTimer();
    };
  }, [clearHoverTimer]);

  return (
    <DropdownMenu open={open} onOpenChange={setOpen} modal={false}>
      <DropdownMenuTrigger asChild>
        <Link
          to={item.to}
          title={item.label}
          className={triggerClassName}
          onClick={() => {
            clearHoverTimer();
            setOpen(false);
          }}
          onMouseEnter={() => scheduleHover(true, FLYOUT_OPEN_DELAY_MS)}
          onMouseLeave={() => scheduleHover(false, FLYOUT_CLOSE_DELAY_MS)}
        >
          <span className={iconClassName}>{item.icon}</span>
        </Link>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        side="right"
        align="start"
        onMouseEnter={clearHoverTimer}
        onMouseLeave={() => scheduleHover(false, FLYOUT_CLOSE_DELAY_MS)}
        className="min-w-48 border-neutral-200 bg-white p-1 text-neutral-900 dark:border-neutral-800 dark:bg-neutral-950 dark:text-neutral-100"
      >
        <DropdownMenuItem asChild>
          <Link
            to={item.to}
            className={cn(menuChildItemClassName, "font-semibold")}
          >
            <span className="flex size-3.5 shrink-0 items-center justify-center text-neutral-500 dark:text-neutral-400">
              {item.icon}
            </span>
            <span className="min-w-0 flex-1 truncate">{item.label}</span>
            {item.badge ? (
              <span className="rounded bg-neutral-200 px-1.5 py-0.5 text-[9px] font-medium uppercase leading-none text-neutral-500 dark:bg-white/[0.06] dark:text-neutral-400">
                {item.badge}
              </span>
            ) : null}
          </Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {(item.children ?? []).map((child) => {
          const childKey = `${groupStorageKey}-${child.to ?? child.label}`;
          const content = (
            <>
              {child.icon ? (
                <span className="flex size-3.5 shrink-0 items-center justify-center text-neutral-500 dark:text-neutral-400">
                  {child.icon}
                </span>
              ) : null}
              <span className="truncate">{child.label}</span>
            </>
          );

          if (child.to && child.external) {
            return (
              <DropdownMenuItem key={childKey} asChild>
                <a
                  href={child.to}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={() => child.onClick?.()}
                  className={menuChildItemClassName}
                >
                  {content}
                </a>
              </DropdownMenuItem>
            );
          }

          if (child.to) {
            return (
              <DropdownMenuItem key={childKey} asChild>
                <Link
                  to={child.to}
                  onClick={() => child.onClick?.()}
                  className={menuChildItemClassName}
                >
                  {content}
                </Link>
              </DropdownMenuItem>
            );
          }

          return (
            <DropdownMenuItem
              key={childKey}
              disabled={child.disabled}
              onSelect={(event) => {
                event.preventDefault();
                child.onClick?.();
              }}
              className={menuChildItemClassName}
            >
              {content}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function SidebarTreeNav({ items, collapsed: collapsedOverride }: Props) {
  const collapsedFromStore = useSidebarStore((state) => state.collapsed);
  const collapsed = collapsedOverride ?? collapsedFromStore;
  const isActive = useIsActive();
  const openGroupsDidMountRef = useRef(false);
  const expandedChildrenDidMountRef = useRef(false);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>(() => {
    const stored = readStoredRecord(OPEN_GROUPS_STORAGE_KEY);
    return {
      ...stored,
      ...Object.fromEntries(
        items
          .filter((item) => item.children && item.children.length > 0)
          .map((item) => {
            const key = getItemStorageKey(item);
            return [
              key,
              stored[key] ?? stored[item.label] ?? resolveDefaultOpen(item),
            ];
          }),
      ),
    };
  });
  const [expandedChildren, setExpandedChildren] = useState<
    Record<string, boolean>
  >(() => {
    const stored = readStoredRecord(EXPANDED_CHILDREN_STORAGE_KEY);
    return {
      ...stored,
      ...Object.fromEntries(
        items
          .filter((item) => item.children && item.children.length > 0)
          .map((item) => {
            const key = getItemStorageKey(item);
            return [key, stored[key] ?? stored[item.label] ?? false];
          }),
      ),
    };
  });

  useEffect(() => {
    if (!openGroupsDidMountRef.current) {
      openGroupsDidMountRef.current = true;
      return;
    }
    writeStoredRecord(OPEN_GROUPS_STORAGE_KEY, openGroups);
  }, [openGroups]);

  useEffect(() => {
    if (!expandedChildrenDidMountRef.current) {
      expandedChildrenDidMountRef.current = true;
      return;
    }
    writeStoredRecord(EXPANDED_CHILDREN_STORAGE_KEY, expandedChildren);
  }, [expandedChildren]);

  return (
    <nav
      className={cn("flex flex-col gap-1 py-1 antialiased", {
        "items-center": collapsed,
      })}
    >
      {items.map((item) => {
        const groupStorageKey = getItemStorageKey(item);
        const childPathsWithQuerySiblings = new Set(
          item.children
            ?.filter((child) => child.to?.includes("?"))
            .map((child) => normalizePath(child.to ?? "")) ?? [],
        );
        const childIsActive = item.children?.some((child) => {
          if (child.active === false || !child.to) {
            return false;
          }
          const requireExactQuery =
            !child.to.includes("?") &&
            childPathsWithQuerySiblings.has(normalizePath(child.to));
          return isActive(child.to, true, requireExactQuery);
        });
        const itemIsActive = isActive(item.to, item.to === "/discover");
        const active = itemIsActive || childIsActive;
        const hasChildren = item.children && item.children.length > 0;
        const open = openGroups[groupStorageKey] ?? resolveDefaultOpen(item);
        const childrenExpanded = expandedChildren[groupStorageKey] ?? false;
        const collapsedPreviewChildCount =
          item.initialVisibleChildren && item.children
            ? Math.min(item.children.length, item.initialVisibleChildren + 1)
            : undefined;
        const previewChildCount = childrenExpanded
          ? undefined
          : collapsedPreviewChildCount;
        const hiddenChildCount =
          collapsedPreviewChildCount && item.children
            ? Math.max(item.children.length - collapsedPreviewChildCount, 0)
            : 0;
        const topLevelClassName = cn(
          "group flex h-7 w-full items-center gap-2.5 rounded-md px-2 text-[13px] font-medium leading-5 text-neutral-600 transition-colors duration-100 hover:bg-neutral-200/70 hover:text-neutral-950 dark:text-neutral-300 dark:hover:bg-white/[0.04] dark:hover:text-neutral-200",
          {
            "w-7 justify-center px-0": collapsed,
            "bg-neutral-200 text-neutral-950 shadow-[inset_0_0_0_1px_rgba(0,0,0,0.04)] dark:bg-white/[0.07] dark:text-neutral-50 dark:shadow-[inset_0_0_0_1px_rgba(255,255,255,0.035)]":
              active && (!hasChildren || collapsed),
            "text-neutral-950 dark:text-neutral-100":
              active && hasChildren && !collapsed,
          },
        );
        const topLevelIconClassName = cn(
          "flex size-4 shrink-0 items-center justify-center text-neutral-500 transition-colors duration-100 group-hover:text-neutral-900 dark:text-neutral-500 dark:group-hover:text-neutral-300",
          {
            "text-neutral-950 dark:text-neutral-50":
              active && (!hasChildren || collapsed),
            "text-neutral-800 dark:text-neutral-200":
              active && hasChildren && !collapsed,
          },
        );
        const menuChildItemClassName =
          "flex cursor-pointer items-center gap-2 text-[13px] font-medium";
        return (
          <div
            key={groupStorageKey}
            className={cn("w-full", {
              "flex justify-center": collapsed,
            })}
          >
            {hasChildren && !collapsed ? (
              <button
                type="button"
                className={topLevelClassName}
                onClick={() => {
                  setOpenGroups((value) => ({
                    ...value,
                    [groupStorageKey]: !open,
                  }));
                }}
              >
                <span className={topLevelIconClassName}>{item.icon}</span>
                <span className="min-w-0 flex-1 truncate text-left">
                  {item.label}
                </span>
                {item.badge ? (
                  <span className="rounded bg-neutral-200 px-1.5 py-0.5 text-[9px] font-medium uppercase leading-none text-neutral-500 dark:bg-white/[0.06] dark:text-neutral-400">
                    {item.badge}
                  </span>
                ) : null}
                <ChevronDownIcon
                  className={cn(
                    "size-3.5 shrink-0 text-neutral-500 transition-transform duration-100 dark:text-neutral-500",
                    {
                      "-rotate-90": !open,
                    },
                  )}
                />
              </button>
            ) : hasChildren && collapsed ? (
              <CollapsedNavItem
                item={item}
                groupStorageKey={groupStorageKey}
                triggerClassName={topLevelClassName}
                iconClassName={topLevelIconClassName}
                menuChildItemClassName={menuChildItemClassName}
              />
            ) : (
              <Link
                to={item.to}
                title={collapsed ? item.label : undefined}
                className={topLevelClassName}
              >
                <span className={topLevelIconClassName}>{item.icon}</span>
                {!collapsed && (
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                )}
              </Link>
            )}

            {!collapsed && hasChildren && open ? (
              <div className="ml-[18px] mt-1 flex flex-col gap-px border-l border-neutral-200 pl-3 dark:border-white/[0.06]">
                {(item.children ?? [])
                  .slice(0, previewChildCount)
                  .map((child, childIndex) => {
                    const partiallyHidden =
                      Boolean(item.initialVisibleChildren) &&
                      !childrenExpanded &&
                      hiddenChildCount > 0 &&
                      childIndex === item.initialVisibleChildren;
                    const childActive =
                      child.active === false || !child.to || child.external
                        ? false
                        : isActive(
                            child.to,
                            true,
                            !child.to.includes("?") &&
                              childPathsWithQuerySiblings.has(
                                normalizePath(child.to),
                              ),
                          );
                    const childClassName = cn(
                      "group/child flex h-7 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] font-medium leading-5 text-neutral-500 transition-colors duration-100 hover:bg-neutral-200/70 hover:text-neutral-950 dark:text-neutral-400 dark:hover:bg-white/[0.04] dark:hover:text-neutral-200",
                      {
                        "bg-neutral-200 text-neutral-950 dark:bg-white/[0.07] dark:text-neutral-50":
                          childActive,
                        "pointer-events-none opacity-50": child.disabled,
                        "opacity-60": partiallyHidden && !child.disabled,
                      },
                    );
                    const childStyle = partiallyHidden
                      ? {
                          maskImage:
                            "linear-gradient(to bottom, black 0%, transparent 100%)",
                          WebkitMaskImage:
                            "linear-gradient(to bottom, black 0%, transparent 100%)",
                        }
                      : undefined;
                    const content = (
                      <>
                        {child.icon ? (
                          <span
                            className={cn(
                              "flex size-3.5 shrink-0 items-center justify-center text-neutral-400 transition-colors duration-100 group-hover/child:text-neutral-900 dark:text-neutral-500 dark:group-hover/child:text-neutral-300",
                              {
                                "text-neutral-950 dark:text-neutral-50":
                                  childActive,
                              },
                            )}
                          >
                            {child.icon}
                          </span>
                        ) : null}
                        <span className="truncate">{child.label}</span>
                      </>
                    );

                    return child.disabled ? (
                      <button
                        key={`${groupStorageKey}-${child.to ?? child.label}`}
                        type="button"
                        disabled
                        className={childClassName}
                        style={childStyle}
                      >
                        {content}
                      </button>
                    ) : child.to && child.external ? (
                      <a
                        key={`${groupStorageKey}-${child.to ?? child.label}`}
                        href={child.to}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={() => child.onClick?.()}
                        className={childClassName}
                        style={childStyle}
                      >
                        {content}
                      </a>
                    ) : child.to ? (
                      <Link
                        key={`${groupStorageKey}-${child.to ?? child.label}`}
                        to={child.to}
                        onClick={() => child.onClick?.()}
                        className={childClassName}
                        style={childStyle}
                      >
                        {content}
                      </Link>
                    ) : (
                      <button
                        key={`${groupStorageKey}-${child.to ?? child.label}`}
                        type="button"
                        className={childClassName}
                        onClick={(event) => {
                          event.stopPropagation();
                          child.onClick?.();
                        }}
                        style={childStyle}
                      >
                        {content}
                      </button>
                    );
                  })}
                {item.initialVisibleChildren &&
                item.children &&
                hiddenChildCount > 0 ? (
                  <button
                    type="button"
                    className="flex h-7 w-full items-center gap-2 rounded-md px-2 text-left text-[13px] font-medium leading-5 text-neutral-500 transition-colors duration-100 hover:bg-neutral-200/70 hover:text-neutral-950 dark:text-neutral-400 dark:hover:bg-white/[0.04] dark:hover:text-neutral-200"
                    onClick={() => {
                      setExpandedChildren((value) => ({
                        ...value,
                        [groupStorageKey]: !childrenExpanded,
                      }));
                    }}
                  >
                    <ChevronDownIcon
                      className={cn(
                        "size-3.5 shrink-0 text-neutral-400 transition-transform duration-100 dark:text-neutral-500",
                        {
                          "rotate-180": childrenExpanded,
                        },
                      )}
                    />
                    {childrenExpanded
                      ? "Show less"
                      : `${hiddenChildCount} more`}
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>
        );
      })}
    </nav>
  );
}

export { SidebarTreeNav };
export type { SidebarNavItem };
