import { useEffect, useRef, useState } from "react";
import {
  CustomSelectItem,
  Select,
  SelectContent,
  SelectItem,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { useGoogleOAuthCredentials } from "@/hooks/useGoogleOAuthCredentials";
import { PlusIcon } from "@radix-ui/react-icons";

type Props = {
  nodeId: string;
  value: string;
  onChange: (value: string) => void;
};

const ADVANCED_OPTION = "__advanced__";
const SETTINGS_OPTION = "__settings__";

function GoogleOAuthCredentialSelector({
  nodeId,
  value,
  onChange,
}: Readonly<Props>) {
  const { credentials, isLoading, isFetching } = useGoogleOAuthCredentials();
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Keep latest callback without forcing effect re-runs.
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // If the value looks like a Jinja template, default to advanced mode
  const isTemplateValue = value.includes("{{") || value.includes("{%");
  const useAdvanced = showAdvanced || isTemplateValue;

  const hasCredentials = credentials.length > 0;
  const isKnownCredential = credentials.some((c) => c.id === value);
  const firstValidId =
    credentials.find((c) => c.valid)?.id ?? credentials[0]?.id;
  const needsAutoFill = !value;

  useEffect(() => {
    if (
      isLoading ||
      isFetching ||
      !hasCredentials ||
      !needsAutoFill ||
      !firstValidId
    ) {
      return;
    }
    onChangeRef.current(firstValidId);
  }, [isLoading, isFetching, hasCredentials, needsAutoFill, firstValidId]);

  const handlePickerValueChange = (selected: string) => {
    if (selected === ADVANCED_OPTION) {
      setShowAdvanced(true);
      return;
    }

    if (selected === SETTINGS_OPTION) {
      window.open("/integrations", "_blank");
      return;
    }

    onChange(selected);
  };

  const handleUseAccountPicker = () => {
    setShowAdvanced(false);

    // Clear templates so view can switch from advanced editor back to picker.
    if (isTemplateValue) {
      onChange("");
    }
  };

  if (isLoading) {
    return <Skeleton className="h-9 w-full" />;
  }

  return (
    <div className="space-y-2">
      {useAdvanced ? (
        <>
          <WorkflowBlockInputTextarea
            nodeId={nodeId}
            value={value}
            onChange={onChange}
            placeholder="{{ google_credential_id }}"
            className="nopan text-xs"
          />
          <button
            type="button"
            onClick={handleUseAccountPicker}
            className="text-xs text-slate-400 underline hover:text-slate-300"
          >
            Use account picker
          </button>
        </>
      ) : (
        <>
          {value && hasCredentials && !isKnownCredential ? (
            <p className="rounded-md border border-amber-600/40 bg-amber-900/20 px-2 py-1 text-[0.7rem] text-amber-200">
              Saved Google account is no longer connected. Pick another below.
            </p>
          ) : null}

          <Select value={value} onValueChange={handlePickerValueChange}>
            <SelectTrigger className="nopan text-xs">
              <SelectValue placeholder="Select a Google account" />
            </SelectTrigger>
            <SelectContent>
              {credentials.map((cred) => (
                <CustomSelectItem key={cred.id} value={cred.id}>
                  <div className="space-y-0.5">
                    <p className="text-sm font-medium">
                      <SelectItemText>{cred.credential_name}</SelectItemText>
                    </p>
                    <p className="text-xs text-slate-400">{cred.id}</p>
                  </div>
                </CustomSelectItem>
              ))}

              <SelectItem value={SETTINGS_OPTION}>
                <div className="flex items-center gap-2">
                  <PlusIcon className="size-4" />
                  <span>Connect new account</span>
                </div>
              </SelectItem>

              <SelectItem value={ADVANCED_OPTION}>
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs">{"{{}}"}</span>
                  <span>Use template expression</span>
                </div>
              </SelectItem>
            </SelectContent>
          </Select>
        </>
      )}
    </div>
  );
}

export { GoogleOAuthCredentialSelector };
