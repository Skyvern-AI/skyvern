import { SaveIcon } from "@/components/icons/SaveIcon";
import { Button } from "@/components/ui/button";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  ExitIcon,
  PlayIcon,
} from "@radix-ui/react-icons";
import { useNavigate, useParams } from "react-router-dom";
import { EditableNodeTitle } from "./nodes/components/EditableNodeTitle";

type Props = {
  title: string;
  parametersPanelOpen: boolean;
  onParametersClick: () => void;
  onSave: () => void;
  onTitleChange: (title: string) => void;
};

function WorkflowHeader({
  title,
  parametersPanelOpen,
  onParametersClick,
  onSave,
  onTitleChange,
}: Props) {
  const { workflowPermanentId } = useParams();
  const navigate = useNavigate();

  return (
    <div className="flex h-full w-full bg-slate-elevation2">
      <div className="flex h-full w-1/3 items-center pl-6">
        <div className="flex">
          <div
            className="cursor-pointer rounded-full p-2 hover:bg-slate-elevation5"
            onClick={() => {
              navigate("/workflows");
            }}
          >
            <ExitIcon className="h-6 w-6" />
          </div>
          <div>
            <div
              className="cursor-pointer rounded-full p-2 hover:bg-slate-elevation5"
              onClick={() => {
                onSave();
              }}
            >
              <SaveIcon />
            </div>
          </div>
        </div>
      </div>
      <div className="flex h-full w-1/3 items-center justify-center p-1">
        <EditableNodeTitle
          editable={true}
          onChange={onTitleChange}
          value={title}
          className="text-3xl"
        />
      </div>
      <div className="flex h-full w-1/3 items-center justify-end gap-4 p-4">
        <Button variant="secondary" size="lg" onClick={onParametersClick}>
          <span className="mr-2">Parameters</span>
          {parametersPanelOpen ? (
            <ChevronUpIcon className="h-6 w-6" />
          ) : (
            <ChevronDownIcon className="h-6 w-6" />
          )}
        </Button>
        <Button
          size="lg"
          onClick={() => {
            navigate(`/workflows/${workflowPermanentId}/run`);
          }}
        >
          <span className="mr-2">Run</span>
          <PlayIcon className="h-6 w-6" />
        </Button>
      </div>
    </div>
  );
}

export { WorkflowHeader };
