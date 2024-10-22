import { Handle, Position } from "@xyflow/react";

function StartNode() {
  return (
    <div>
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <div className="w-[30rem] rounded-lg bg-slate-elevation3 px-6 py-4 text-center">
        Start
      </div>
    </div>
  );
}

export { StartNode };
