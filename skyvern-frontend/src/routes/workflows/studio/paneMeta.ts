import { type ComponentType } from "react";
import {
  ChatBubbleIcon,
  GlobeIcon,
  ReaderIcon,
  Share1Icon,
} from "@radix-ui/react-icons";

import { type StudioPaneId } from "./panes";

export const STUDIO_PANE_META: Record<
  StudioPaneId,
  { label: string; icon: ComponentType<{ className?: string }> }
> = {
  copilot: { label: "Copilot", icon: ChatBubbleIcon },
  editor: { label: "Editor", icon: Share1Icon },
  browser: { label: "Browser", icon: GlobeIcon },
  overview: { label: "Overview", icon: ReaderIcon },
};
