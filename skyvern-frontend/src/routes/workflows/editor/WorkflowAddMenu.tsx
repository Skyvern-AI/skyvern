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
  canUploadSOP?: boolean;
  canRecordTask?: boolean;
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
  canUploadSOP = true,
  canRecordTask = true,
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

    // Show Demonstrate task whenever a browser session exists (ready or still
    // connecting). Disable it until the browser is actually ready so users
    // can see the action will be available without it popping in mid-load.
    if (settingsStore.isUsingABrowser || settingsStore.isLoadingABrowser) {
      menuItems.push({
        id: "2",
        icon: <SquareIcon className={buttonSize ? "h-3 w-3" : undefined} />,
        enabled:
          canRecordTask &&
          settingsStore.isUsingABrowser &&
          !recordingStore.isRecording,
        text: "Demonstrate task",
        onClick: () => {
          onRecord();
        },
      });
    }

    menuItems.push({
      id: "3",
      icon: <UploadIcon className={buttonSize ? "h-3 w-3" : undefined} />,
      text: "Upload SOP",
      enabled: canUploadSOP,
      onClick: () => {
        onUploadSOP();
      },
    });

    return menuItems;
  }, [
    buttonSize,
    canRecordTask,
    onAdd,
    onRecord,
    onUploadSOP,
    recordingStore.isRecording,
    settingsStore.isUsingABrowser,
    settingsStore.isLoadingABrowser,
    canUploadSOP,
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
