import { UpdateIcon } from "@radix-ui/react-icons";

import { Tip } from "@/components/Tip";

type Props = {
  retriedFromWorkflowRunId: string | null | undefined;
};

const retryConfig = {
  icon: <UpdateIcon className="size-3.5 text-blue-400" />,
  label: "Automatic retry with fallback credential",
};

function CredentialFallbackRetryBadge({ retriedFromWorkflowRunId }: Props) {
  if (!retriedFromWorkflowRunId) {
    return null;
  }

  return (
    <Tip content={retryConfig.label}>
      <span
        aria-label={retryConfig.label}
        className="inline-flex shrink-0 items-center"
      >
        {retryConfig.icon}
      </span>
    </Tip>
  );
}

export { CredentialFallbackRetryBadge };
