import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
  DrawerTrigger,
} from "@/components/ui/drawer";
import { HamburgerMenuIcon } from "@radix-ui/react-icons";
import { SidebarContent } from "./SidebarContent";
import * as VisuallyHidden from "@radix-ui/react-visually-hidden";

function NavigationHamburgerMenu() {
  return (
    <div className="block lg:hidden">
      <Drawer direction="left">
        <DrawerTrigger asChild>
          <HamburgerMenuIcon className="size-6 cursor-pointer" />
        </DrawerTrigger>
        <DrawerContent className="bottom-2 left-2 top-2 mt-0 h-full w-64 rounded border-0">
          <VisuallyHidden.Root>
            <DrawerHeader>
              <DrawerTitle>Skyvern</DrawerTitle>
              <DrawerDescription>Skyvern App Navigation</DrawerDescription>
            </DrawerHeader>
          </VisuallyHidden.Root>
          <SidebarContent />
        </DrawerContent>
      </Drawer>
    </div>
  );
}

export { NavigationHamburgerMenu };
