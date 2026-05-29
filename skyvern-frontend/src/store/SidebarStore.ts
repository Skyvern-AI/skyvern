import { create } from "zustand";

type SidebarStore = {
  collapsed: boolean;
  setCollapsed: (collapsed: boolean) => void;
};

const SIDEBAR_COLLAPSED_STORAGE_KEY = "skyvern-sidebar-collapsed";

function getInitialCollapsed() {
  try {
    if (typeof window === "undefined") {
      return false;
    }
    return (
      window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === "true"
    );
  } catch {
    return false;
  }
}

function persistCollapsed(collapsed: boolean) {
  try {
    if (typeof window === "undefined") {
      return;
    }
    window.localStorage.setItem(
      SIDEBAR_COLLAPSED_STORAGE_KEY,
      String(collapsed),
    );
  } catch {
    // Persistence is cosmetic; keep the in-memory collapse state if storage
    // is blocked.
  }
}

const useSidebarStore = create<SidebarStore>((set) => {
  return {
    collapsed: getInitialCollapsed(),
    setCollapsed: (collapsed: boolean) => {
      persistCollapsed(collapsed);
      set({ collapsed });
    },
  };
});

export { useSidebarStore };
