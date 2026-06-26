import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { ExclamationTriangleIcon } from "@radix-ui/react-icons";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CredentialSetupTelemetry } from "@/util/onboarding/credentialSetupTelemetry";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { workflowEditorPath } from "@/routes/workflows/studioNavigation";

type CredentialSetupPromptProps = {
  workflowPermanentId: string;
  blocksMissingCredentials: Array<{ label: string }>;
};

/**
 * Activation-surface credential prompt shown on the run-parameters page when a
 * workflow's login block(s) still need a credential. The CTA routes into the
 * in-editor login-node credential experience (the only surface that binds a
 * credential to the block); opening the credential modal here instead would
 * orphan the new credential, since binding writes to the editor-only store.
 */
function CredentialSetupPrompt({
  workflowPermanentId,
  blocksMissingCredentials,
}: Readonly<CredentialSetupPromptProps>) {
  const blockCount = blocksMissingCredentials.length;
  const studioEnabled = useWorkflowStudioEnabled();
  const shownRef = useRef(false);

  useEffect(() => {
    if (blockCount === 0 || shownRef.current) return;
    shownRef.current = true;
    CredentialSetupTelemetry.credentialSetupShown("run_parameters", blockCount);
  }, [blockCount]);

  if (blockCount === 0) {
    return null;
  }

  return (
    <Alert variant="warning" data-testid="credential-setup-prompt">
      <ExclamationTriangleIcon className="h-4 w-4" />
      <AlertTitle>Set up login credentials to run this agent</AlertTitle>
      <AlertDescription>
        <p>
          This agent signs in to a site, so it needs a saved credential before
          its first run. These login step(s) still need one:
        </p>
        <ul className="mt-2 list-inside list-disc">
          {blocksMissingCredentials.map((block, index) => (
            <li key={index}>{block.label}</li>
          ))}
        </ul>
        <Button asChild size="sm" className="mt-3">
          <Link
            to={workflowEditorPath(workflowPermanentId, studioEnabled)}
            onClick={() =>
              CredentialSetupTelemetry.credentialSetupCtaClicked(
                "run_parameters",
                blockCount,
              )
            }
          >
            Set up credentials in the editor
          </Link>
        </Button>
      </AlertDescription>
    </Alert>
  );
}

export { CredentialSetupPrompt };
export type { CredentialSetupPromptProps };
