import { NavigationHamburgerMenu } from "./NavigationHamburgerMenu";
import { useSidebarHidden } from "./useSidebarHidden";
import { Button } from "@/components/ui/button";
import { useContext } from "react";
import { OidcAuthContext } from "@/store/OnPremOidcContext";

function Header() {
  const sidebarHidden = useSidebarHidden({ revealOnNotFound: true });
  const { enabled, user, signOut } = useContext(OidcAuthContext);

  if (sidebarHidden) {
    return null;
  }

  return (
    <header>
      <div className="flex h-24 items-center justify-between px-6">
        <NavigationHamburgerMenu />
        {enabled && user && (
          <Button variant="ghost" onClick={() => void signOut()}>
            Sign out
          </Button>
        )}
      </div>
    </header>
  );
}

export { Header };
