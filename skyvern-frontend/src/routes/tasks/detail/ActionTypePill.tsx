import { ActionType, ReadableActionTypes } from "@/api/types";
import { StatusPill } from "@/components/ui/status-pill";
import {
  CursorArrowIcon,
  HandIcon,
  DownloadIcon,
  InputIcon,
} from "@radix-ui/react-icons";

type Props = {
  actionType: ActionType;
};

const icons: Partial<Record<ActionType, React.ReactNode>> = {
  click: <CursorArrowIcon className="h-4 w-4" />,
  hover: <HandIcon className="h-4 w-4" />,
  input_text: <InputIcon className="h-4 w-4" />,
  download_file: <DownloadIcon className="h-4 w-4" />,
};

function ActionTypePill({ actionType }: Props) {
  return (
    <StatusPill icon={icons[actionType] ?? null}>
      {ReadableActionTypes[actionType]}
    </StatusPill>
  );
}

export { ActionTypePill };
