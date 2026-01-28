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

    // Only show Record Browser when browser is ON
    if (settingsStore.isUsingABrowser) {
      menuItems.push({
        id: "2",
        icon: <SquareIcon className={buttonSize ? "h-3 w-3" : undefined} />,
        enabled: !recordingStore.isRecording,
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
    isUploadingSOP,
  ]);

  // Show menu in debug mode regardless of browser state
  if (!debugStore.isDebugMode) {
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
    >
      {children}
    </RadialMenu>
  );
}

export { WorkflowAddMenu };
