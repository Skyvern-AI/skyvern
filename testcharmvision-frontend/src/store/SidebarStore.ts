import { create } from "zustand";

type SidebarStore = {
  collapsed: boolean;
  setCollapsed: (collapsed: boolean) => void;
};

const useSidebarStore = create<SidebarStore>((set) => {
  return {
    collapsed: false,
    setCollapsed: (collapsed: boolean) => set({ collapsed }),
  };
});

export { useSidebarStore };
