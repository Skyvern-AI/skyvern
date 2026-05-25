import { PlusIcon, ReloadIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { useCreateBrowserSessionMutation } from "@/routes/browserSessions/hooks/useCreateBrowserSessionMutation";
import { useBrowserProfileCreateStore } from "@/store/useBrowserProfileCreateStore";

type Props = {
  size?: "default" | "lg";
  label?: string;
};

function CreateBrowserProfileButton({
  size = "default",
  label = "Create a Browser Profile",
}: Props) {
  const createBrowserSessionMutation = useCreateBrowserSessionMutation();
  const isBackgroundCreateInProgress = useBrowserProfileCreateStore(
    (state) => state.active !== null,
  );

  const disabled =
    createBrowserSessionMutation.isPending || isBackgroundCreateInProgress;

  return (
    <Button
      variant="brand"
      size={size}
      disabled={disabled}
      title={
        isBackgroundCreateInProgress
          ? "A browser profile is already being created"
          : undefined
      }
      onClick={() => {
        createBrowserSessionMutation.mutate({
          proxyLocation: null,
          timeout: null,
        });
      }}
    >
      {createBrowserSessionMutation.isPending ? (
        <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
      ) : (
        <PlusIcon className="mr-2 h-4 w-4" />
      )}
      {label}
    </Button>
  );
}

export { CreateBrowserProfileButton };
