import { useSupabaseAuth } from "@/store/SupabaseAuthContext";
import { isSupabaseEnabled } from "@/api/supabase";
import { DebugStoreProvider } from "@/store/DebugStoreContext";
import { RootLayout } from "./RootLayout";
import { LandingPage } from "@/routes/landing/LandingPage";
import { useLocation } from "react-router-dom";

export function ConditionalRoot() {
  const { user, loading } = useSupabaseAuth();
  const location = useLocation();

  // If Supabase is not enabled, always show the app
  if (!isSupabaseEnabled) {
    return (
      <DebugStoreProvider>
        <RootLayout />
      </DebugStoreProvider>
    );
  }

  // Show loading state
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950">
        <div className="text-muted-foreground">로딩 중...</div>
      </div>
    );
  }

  // If not authenticated and on root path, show landing page
  if (!user && location.pathname === "/") {
    return <LandingPage />;
  }

  // If not authenticated but trying to access other routes, redirect to login
  if (!user) {
    window.location.href = "/login";
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-950">
        <div className="text-muted-foreground">로딩 중...</div>
      </div>
    );
  }

  // Authenticated user - show the app
  return (
    <DebugStoreProvider>
      <RootLayout />
    </DebugStoreProvider>
  );
}
