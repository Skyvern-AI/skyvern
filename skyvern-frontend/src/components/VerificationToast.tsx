import { LockClosedIcon, ExternalLinkIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";

type ToastContentProps = {
  label: string;
  navigateUrl?: string;
};

function VerificationToastContent({ label, navigateUrl }: ToastContentProps) {
  return (
    <div className="space-y-2">
      <div className="flex items-start gap-2 font-medium">
        <LockClosedIcon className="mt-0.5 h-4 w-4 flex-shrink-0 text-warning" />
        <span>2FA Code Required</span>
      </div>
      <p className="text-muted-foreground">
        {label} needs verification to continue.
      </p>
      {navigateUrl && (
        <Link
          to={navigateUrl}
          className="inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
        >
          Go to workflow
          <ExternalLinkIcon className="h-3 w-3" />
        </Link>
      )}
    </div>
  );
}

export { VerificationToastContent };
