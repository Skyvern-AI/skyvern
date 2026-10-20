import type { ReactNode } from "react";

import { usePageSlots } from "@/store/PageSlots";

type Props = {
  children: ReactNode;
};

function CreatorDirectoryBoundary({ children }: Props) {
  const { workflowCreatorDirectory: CreatorDirectory } = usePageSlots();
  return CreatorDirectory ? (
    <CreatorDirectory>{children}</CreatorDirectory>
  ) : (
    <>{children}</>
  );
}

export { CreatorDirectoryBoundary };
