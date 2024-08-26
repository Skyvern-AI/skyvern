import { Button } from "@/components/ui/button";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  ExitIcon,
  PlayIcon,
} from "@radix-ui/react-icons";
import { useNavigate, useParams } from "react-router-dom";

type Props = {
  title: string;
  parametersPanelOpen: boolean;
  onParametersClick: () => void;
};

function WorkflowHeader({
  title,
  parametersPanelOpen,
  onParametersClick,
}: Props) {
  const { workflowPermanentId } = useParams();
  const navigate = useNavigate();

  return (
    <div className="flex h-full w-full bg-slate-elevation2">
      <div className="flex h-full w-1/3 items-center pl-6">
        <div
          className="cursor-pointer rounded-full p-2 hover:bg-slate-elevation5"
          onClick={() => {
            navigate("/workflows");
          }}
        >
          <ExitIcon className="h-6 w-6" />
        </div>
      </div>
      <div className="flex h-full w-1/3 items-center justify-center">
        <span className="max-w-max truncate text-3xl">{title}</span>
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
