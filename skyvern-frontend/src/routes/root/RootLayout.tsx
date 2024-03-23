import { Outlet } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { Toaster } from "@/components/ui/toaster";

function RootLayout() {
  return (
    <>
      <div className="flex w-full h-full">
        <Sidebar />
        <Outlet />
        <aside className="w-72 shrink-0"></aside>
      </div>
      <Toaster />
    </>
  );
}

export { RootLayout };
