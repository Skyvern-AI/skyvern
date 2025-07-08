import { Toaster } from "@/components/ui/toaster";
import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import { Outlet } from "react-router-dom";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";
import { useDebugStore } from "@/store/useDebugStore";

function RootLayout() {
  const collapsed = useSidebarStore((state) => state.collapsed);
  const embed = new URLSearchParams(window.location.search).get("embed");
  const isEmbedded = embed === "true";
  const debugStore = useDebugStore();

  return (
    <>
      {!isEmbedded && <Sidebar />}
      <div className="h-full w-full">
        <Header />
        <main
          className={cn("lg:pb-4 lg:pl-64", {
            "lg:pl-28": collapsed,
            "lg:pl-4": isEmbedded,
            "lg:pb-0": debugStore.isDebugMode,
          })}
        >
          <Outlet />
        </main>
      </div>
      <Toaster />
    </>
  );
}

export { RootLayout };
