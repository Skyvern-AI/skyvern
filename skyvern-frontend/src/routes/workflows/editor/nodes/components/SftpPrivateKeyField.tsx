import { useEffect, useRef, useState } from "react";

import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Button } from "@/components/ui/button";
import { isTemplateExpression } from "@/util/googleSheetsUrl";

type SftpPrivateKeyFieldProps = {
  nodeId: string;
  value: string | null | undefined;
  onChange: (value: string) => boolean | void;
  disabled?: boolean;
  className?: string;
};

function SftpPrivateKeyField({
  nodeId,
  value,
  onChange,
  disabled,
  className,
}: SftpPrivateKeyFieldProps) {
  const [replacement, setReplacement] = useState<string | null>(null);
  const lastValue = useRef(value);

  useEffect(() => {
    if (lastValue.current !== value) {
      lastValue.current = value;
      setReplacement(null);
    }
  }, [value]);

  if (value && !isTemplateExpression(value) && replacement === null) {
    return (
      <div className="space-y-2">
        <p className="text-xs text-muted-foreground" role="status">
          A private key is saved and hidden.
        </p>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            onClick={() => setReplacement("")}
          >
            Replace key
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={disabled}
            onClick={() => {
              if (onChange("") !== false) setReplacement("");
            }}
          >
            Remove key
          </Button>
        </div>
      </div>
    );
  }

  return (
    <WorkflowBlockInputTextarea
      name="sftpPrivateKey"
      nodeId={nodeId}
      onChange={(next) => {
        setReplacement(next);
        if (onChange(next) !== false) lastValue.current = next;
      }}
      value={replacement ?? value ?? ""}
      disabled={disabled}
      className={className}
    />
  );
}

export { SftpPrivateKeyField };
