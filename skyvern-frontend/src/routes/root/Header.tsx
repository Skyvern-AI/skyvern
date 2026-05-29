import { NavigationHamburgerMenu } from "./NavigationHamburgerMenu";
import { useSidebarHidden } from "./useSidebarHidden";

function Header() {
  const sidebarHidden = useSidebarHidden();

  if (sidebarHidden) {
    return null;
  }

  return (
    <header>
      <div className="flex h-24 items-center px-6">
        <NavigationHamburgerMenu />
      </div>
    </header>
  );
}

export { Header };
