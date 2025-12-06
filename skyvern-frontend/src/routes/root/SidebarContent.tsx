import { Logo } from "@/components/Logo";
import { LogoMinimized } from "@/components/LogoMinimized";
import { useSidebarStore } from "@/store/SidebarStore";
import { Link, useNavigate } from "react-router-dom";
import { SideNav } from "./SideNav";
import { cn } from "@/util/utils";
import { Button } from "@/components/ui/button";
import { ExitIcon, PinLeftIcon, PinRightIcon } from "@radix-ui/react-icons";
import { useSupabaseAuth } from "@/store/SupabaseAuthContext";
import { isSupabaseEnabled } from "@/api/supabase";

type Props = {
  useCollapsedState?: boolean;
};

function SidebarContent({ useCollapsedState }: Props) {
  const { collapsed: collapsedState, setCollapsed } = useSidebarStore();
  const collapsed = useCollapsedState ? collapsedState : false;
  const { user, signOut } = useSupabaseAuth();
  const navigate = useNavigate();

  const handleSignOut = async () => {
    await signOut();
    navigate("/login");
  };

  return (
    <div className="flex h-full flex-col overflow-y-auto px-6">
      <Link to={window.location.origin}>
        <div className="flex h-24 items-center justify-center">
          {collapsed ? <LogoMinimized /> : <Logo />}
        </div>
      </Link>
      <SideNav />

      {/* User Profile & Logout */}
      {isSupabaseEnabled && user && (
        <div className={cn("mt-auto border-t pt-4", {
          "text-center": collapsed,
        })}>
          {!collapsed && (
            <div className="mb-2 truncate text-sm text-muted-foreground">
              {user.email}
            </div>
          )}
          <Button
            variant="ghost"
            size={collapsed ? "icon" : "default"}
            className={cn("text-muted-foreground hover:text-foreground", {
              "w-full justify-start": !collapsed,
            })}
            onClick={handleSignOut}
          >
            <ExitIcon className="h-4 w-4" />
            {!collapsed && <span className="ml-2">로그아웃</span>}
          </Button>
        </div>
      )}

      <div
        className={cn("mt-4 flex min-h-16", {
          "justify-center": collapsed,
          "justify-end": !collapsed,
          "mt-auto": !isSupabaseEnabled || !user,
        })}
      >
        <Button
          size="icon"
          variant="ghost"
          onClick={() => {
            setCollapsed(!collapsed);
          }}
        >
          {collapsed ? (
            <PinRightIcon className="h-6 w-6" />
          ) : (
            <PinLeftIcon className="hidden h-6 w-6 lg:block" />
          )}
        </Button>
      </div>
    </div>
  );
}

export { SidebarContent };
