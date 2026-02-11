import { ActionType, ReadableActionTypes } from "@/api/types";
import {
  CheckCircledIcon,
  CursorArrowIcon,
  HandIcon,
  DownloadIcon,
  InputIcon,
  QuestionMarkIcon,
} from "@radix-ui/react-icons";
import { Tip } from "@/components/Tip";

type Props = {
  actionType: ActionType;
};

const icons: Partial<Record<ActionType, React.ReactNode>> = {
  click: <CursorArrowIcon className="h-4 w-4" />,
  hover: <HandIcon className="h-4 w-4" />,
  complete: <CheckCircledIcon className="h-4 w-4" />,
  input_text: <InputIcon className="h-4 w-4" />,
  download_file: <DownloadIcon className="h-4 w-4" />,
};

function ActionTypePillMinimal({ actionType }: Props) {
  const icon = icons[actionType] ?? <QuestionMarkIcon className="h-4 w-4" />;

  if (!icon) {
    return null;
  }

  return (
    <Tip content={ReadableActionTypes[actionType]}>
      <div className="flex w-full items-center justify-center gap-2">
        {icon}
      </div>
    </Tip>
  );
}

export { ActionTypePillMinimal };
