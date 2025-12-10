import { CompassIcon } from "@/components/icons/CompassIcon";
import { NavLinkGroup } from "@/components/NavLinkGroup";
import { useSidebarStore } from "@/store/SidebarStore";
import { cn } from "@/util/utils";
import {
  CounterClockwiseClockIcon,
  GearIcon,
  GlobeIcon,
  LightningBoltIcon,
} from "@radix-ui/react-icons";
import { KeyIcon } from "@/components/icons/KeyIcon.tsx";

function SideNav() {
  const { collapsed } = useSidebarStore();

  return (
    <nav
      className={cn("space-y-5", {
        "items-center": collapsed,
      })}
    >
      <NavLinkGroup
        title="만들기"
        links={[
          {
            label: "둘러보기",
            to: "/discover",
            icon: <CompassIcon className="size-6" />,
          },
          {
            label: "워크플로우",
            to: "/workflows",
            icon: <LightningBoltIcon className="size-6" />,
          },
          {
            label: "실행 기록",
            to: "/runs",
            icon: <CounterClockwiseClockIcon className="size-6" />,
          },
          {
            label: "브라우저",
            to: "/browser-sessions",
            icon: <GlobeIcon className="size-6" />,
          },
        ]}
      />
      <NavLinkGroup
        title={"일반"}
        links={[
          {
            label: "설정",
            to: "/settings",
            icon: <GearIcon className="size-6" />,
          },
          {
            label: "인증 정보",
            to: "/credentials",
            icon: <KeyIcon className="size-6" />,
          },
        ]}
      />
    </nav>
  );
}

export { SideNav };
