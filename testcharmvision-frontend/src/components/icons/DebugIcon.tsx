import { PaperPlaneIcon } from "@radix-ui/react-icons";

import { cn } from "@/util/utils";
import { BugIcon } from "./BugIcon";
import { Tip } from "../Tip";

type Props = {
  className?: string;
  // --
  onClick?: () => void;
};

function DebugIcon({ className, onClick }: Props) {
  return (
    <Tip content="Debug (pre-convo-UI)">
      <div
        className={cn("relative flex items-center justify-center", className)}
        onClick={onClick}
      >
        <PaperPlaneIcon className="size-6 cursor-pointer" />
        <div className="absolute right-[-0.75rem] top-[-0.75rem] origin-center rotate-45 text-[#ff7e7e]">
          <BugIcon className="scale-75" />
        </div>
      </div>
    </Tip>
  );
}

export { DebugIcon };
