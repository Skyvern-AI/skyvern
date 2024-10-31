import { Toaster } from "@/components/ui/toaster";
import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import { Outlet } from "react-router-dom";
import { Header } from "./Header";
import { Sidebar } from "./Sidebar";

function RootLayout() {
  const collapsed = useSidebarStore((state) => state.collapsed);

  return (
    <>
      <div className="h-full w-full">
        <Sidebar />
        <Header />
        <main
          className={cn("lg:pb-4 lg:pl-64", {
            "lg:pl-28": collapsed,
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
