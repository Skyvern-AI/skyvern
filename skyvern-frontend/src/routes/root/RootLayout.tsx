import { Outlet } from "react-router-dom";
import { Toaster } from "@/components/ui/toaster";
import { Sidebar } from "./Sidebar";

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
