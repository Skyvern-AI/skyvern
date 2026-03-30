import { PlusIcon, EyeOpenIcon, EyeClosedIcon } from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";
import { cn } from "@/util/utils";
import { Input } from "./ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { WorkflowBlockParameterSelect } from "@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect";
import { useParameterAutocomplete } from "@/hooks/useParameterAutocomplete";
import { ParameterAutocompleteDropdown } from "./ParameterAutocompleteDropdown";
import { ParameterGhostText } from "./ParameterGhostText";

type Props = Omit<React.ComponentProps<typeof Input>, "onChange"> & {
  onChange: (value: string) => void;
  nodeId: string;
  hideParameterSelect?: boolean;
};

function WorkflowBlockInput(props: Props) {
  const { nodeId, onChange, type, value, hideParameterSelect, ...inputProps } =
    props;
  const [showPassword, setShowPassword] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Buffer input value locally so keystrokes update the DOM immediately
  // without waiting for the React Flow store roundtrip (which can reset the
  // cursor position — especially in nodes like ConditionalNode that replace
  // complex nested data on every keystroke).
  const [localValue, setLocalValue] = useState(value ?? "");
  const isLocalChangeRef = useRef(false);

  useEffect(() => {
    if (!isLocalChangeRef.current) {
      setLocalValue(value ?? "");
    }
    isLocalChangeRef.current = false;
  }, [value]);

  const isPasswordField = type === "password";
  const actualType = isPasswordField && showPassword ? "text" : type;

  const autocomplete = useParameterAutocomplete({
    nodeId,
    value: String(localValue),
    inputRef,
    variant: "input",
  });

  const handleAutocompleteSelect = (key: string) => {
    const { newValue, cursorPos } = autocomplete.buildSelectedValue(key);
    isLocalChangeRef.current = true;
    setLocalValue(newValue);
    onChange(newValue);
    autocomplete.dismiss();
    setTimeout(() => {
      if (inputRef.current) {
        inputRef.current.focus();
        inputRef.current.setSelectionRange(cursorPos, cursorPos);
      }
    }, 0);
  };

  return (
    <div className="relative">
      <Input
        {...inputProps}
        ref={inputRef}
        value={localValue}
        type={actualType}
        className={cn(
          isPasswordField ? "pr-18" : hideParameterSelect ? "" : "pr-9",
          props.className,
        )}
        onChange={(event) => {
          isLocalChangeRef.current = true;
          setLocalValue(event.target.value);
          onChange(event.target.value);
        }}
        onKeyDown={(e) => {
          if (autocomplete.isOpen) {
            const handled = autocomplete.handleKeyDown(e);
            if (handled && (e.key === "Enter" || e.key === "Tab")) {
              const param = autocomplete.getSelectedParameter();
              if (param) {
                handleAutocompleteSelect(param.key);
              }
            }
          }
        }}
      />
      <ParameterGhostText
        ghostText={autocomplete.ghostText}
        textBeforeCursor={autocomplete.textBeforeCursor}
        inputRef={inputRef}
        variant="input"
      />
      <ParameterAutocompleteDropdown
        items={autocomplete.filteredItems}
        selectedIndex={autocomplete.selectedIndex}
        anchorPosition={autocomplete.anchorPosition}
        visible={autocomplete.isOpen}
        onSelect={handleAutocompleteSelect}
        onDismiss={autocomplete.dismiss}
      />
      {(isPasswordField || !hideParameterSelect) && (
        <div className="absolute right-0 top-0 flex cursor-pointer items-center justify-center">
          {isPasswordField && (
            <div className="flex size-9 items-center justify-center">
              <div
                className="rounded p-1 hover:bg-muted"
                onClick={() => setShowPassword(!showPassword)}
              >
                {showPassword ? (
                  <EyeClosedIcon className="size-4" />
                ) : (
                  <EyeOpenIcon className="size-4" />
                )}
              </div>
            </div>
          )}
          {!hideParameterSelect && (
            <div className="flex size-9 items-center justify-center">
              <Popover>
                <PopoverTrigger asChild>
                  <div className="rounded p-1 hover:bg-muted">
                    <PlusIcon className="size-4" />
                  </div>
                </PopoverTrigger>
                <PopoverContent className="w-fit max-w-sm">
                  <WorkflowBlockParameterSelect
                    nodeId={nodeId}
                    onAdd={(parameterKey) => {
                      const newValue = `${localValue}{{${parameterKey}}}`;
                      isLocalChangeRef.current = true;
                      setLocalValue(newValue);
                      onChange(newValue);
                    }}
                  />
                </PopoverContent>
              </Popover>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export { WorkflowBlockInput };
