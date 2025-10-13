import { Toaster } from "@/components/ui/toaster";
import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import { Outlet } from "react-router-dom";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";
import { useDebugStore } from "@/store/useDebugStore";
import { SelfHealApiKeyBanner } from "@/components/SelfHealApiKeyBanner";

function RootLayout() {
  const collapsed = useSidebarStore((state) => state.collapsed);
  const embed = new URLSearchParams(window.location.search).get("embed");
  const isEmbedded = embed === "true";
  const debugStore = useDebugStore();

  const horizontalPadding = cn("lg:pl-64", {
    "lg:pl-28": collapsed,
    "lg:pl-4": isEmbedded,
  });

  return (
    <>
      {!isEmbedded && <Sidebar />}
      <div className="h-full w-full">
        <div className={horizontalPadding}>
          <SelfHealApiKeyBanner />
        </div>
        <Header />
        <main
          className={cn("lg:pb-4", horizontalPadding, {
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
