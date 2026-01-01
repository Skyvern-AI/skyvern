import { ActionType, ReadableActionTypes } from "@/api/types";
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
    <div className="flex gap-1 rounded-sm bg-slate-elevation5 px-2 py-1">
      {icons[actionType] ?? null}
      <span className="text-xs">{ReadableActionTypes[actionType]}</span>
    </div>
  );
}

export { ActionTypePill };
