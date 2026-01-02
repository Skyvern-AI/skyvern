import { SquareIcon, PlusIcon } from "@radix-ui/react-icons";
import { ReactNode } from "react";

import { RadialMenu } from "@/components/RadialMenu";
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
  //   --
  onAdd: () => void;
  onRecord: () => void;
};

function WorkflowAddMenu({
  buttonSize,
  children,
  gap,
  radius = "80px",
  rotateText = true,
  startAt = 90,
  //   --
  onAdd,
  onRecord,
}: WorkflowAddMenuProps) {
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const settingsStore = useSettingsStore();

  if (!debugStore.isDebugMode || !settingsStore.isUsingABrowser) {
    return <>{children}</>;
  }

  return (
    <RadialMenu
      items={[
        {
          id: "1",
          icon: <PlusIcon className={buttonSize ? "h-3 w-3" : undefined} />,
          text: "Add Block",
          onClick: () => {
            onAdd();
          },
        },
        {
          id: "2",
          icon: <SquareIcon className={buttonSize ? "h-3 w-3" : undefined} />,
          enabled: !recordingStore.isRecording && settingsStore.isUsingABrowser,
          text: "Record Browser",
          onClick: () => {
            if (!settingsStore.isUsingABrowser) {
              return;
            }

            onRecord();
          },
        },
      ]}
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
