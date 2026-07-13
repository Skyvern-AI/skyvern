import { SquareIcon, PlusIcon, UploadIcon } from "@radix-ui/react-icons";
import { ReactNode, useMemo } from "react";

import { RadialMenu, RadialMenuItem } from "@/components/RadialMenu";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";

type WorkflowAddMenuProps = {
  buttonSize?: string;
  children: ReactNode;
  gap?: number;
  radius?: string;
  rotateText?: boolean;
  startAt?: number;
  isUploadingSOP?: boolean;
  //   --
  onAdd: () => void;
  onRecord: () => void;
  onUploadSOP: () => void;
  onPinnedChange?: (pinned: boolean) => void;
};

function WorkflowAddMenu({
  buttonSize,
  children,
  gap,
  radius = "80px",
  rotateText = true,
  startAt = 90,
  isUploadingSOP = false,
  //   --
  onAdd,
  onRecord,
  onUploadSOP,
  onPinnedChange,
}: WorkflowAddMenuProps) {
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const settingsStore = useSettingsStore();

  const items = useMemo(() => {
    const menuItems: Array<RadialMenuItem> = [
      {
        id: "1",
        icon: <PlusIcon className={buttonSize ? "h-3 w-3" : undefined} />,
        text: "Add Block",
        onClick: () => {
          onAdd();
        },
      },
    ];

    // Show Record Browser whenever a browser session exists (ready or still
    // connecting). Disable it until the browser is actually ready so users
    // can see the action will be available without it popping in mid-load.
    if (settingsStore.isUsingABrowser || settingsStore.isLoadingABrowser) {
      menuItems.push({
        id: "2",
        icon: <SquareIcon className={buttonSize ? "h-3 w-3" : undefined} />,
        enabled: settingsStore.isUsingABrowser && !recordingStore.isRecording,
        text: "Record Browser",
        onClick: () => {
          onRecord();
        },
      });
    }

    // Always show Upload SOP option
    menuItems.push({
      id: "3",
      icon: <UploadIcon className={buttonSize ? "h-3 w-3" : undefined} />,
      text: "Upload SOP",
      enabled: !isUploadingSOP,
      onClick: () => {
        onUploadSOP();
      },
    });

    return menuItems;
  }, [
    buttonSize,
    onAdd,
    onRecord,
    onUploadSOP,
    recordingStore.isRecording,
    settingsStore.isUsingABrowser,
    settingsStore.isLoadingABrowser,
    isUploadingSOP,
  ]);

  // The studio editor (/studio, blockRunsEnabled) surfaces these actions too;
  // gating on blockRunsEnabled avoids flipping isDebugMode, which would lock the
  // canvas zoom/pan. Mirrors NodeHeader's block-run gate.
  if (!debugStore.isDebugMode && !debugStore.blockRunsEnabled) {
    return <>{children}</>;
  }

  return (
    <RadialMenu
      items={items}
      buttonSize={buttonSize}
      radius={radius}
      startAt={startAt}
      gap={gap}
      rotateText={rotateText}
      layout="vertical"
      onPinnedChange={onPinnedChange}
    >
      {children}
    </RadialMenu>
  );
}

export { WorkflowAddMenu };
