import { Navigate, useLocation } from "react-router-dom";
import { useSupabaseAuth } from "@/store/SupabaseAuthContext";
import { isSupabaseEnabled } from "@/api/supabase";

interface AuthGuardProps {
  children: React.ReactNode;
}

export function AuthGuard({ children }: AuthGuardProps) {
  const { user, loading } = useSupabaseAuth();
  const location = useLocation();

  // If Supabase is not enabled, allow access without auth
  if (!isSupabaseEnabled) {
    return <>{children}</>;
  }

  // Show loading state
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="text-muted-foreground">로딩 중...</div>
      </div>
    );
  }

  // Redirect to login if not authenticated
  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
}
