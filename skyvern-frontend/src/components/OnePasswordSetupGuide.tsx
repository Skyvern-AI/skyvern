import { useState, type ReactNode } from "react";
import {
  ChevronDownIcon,
  ExternalLinkIcon,
  InfoCircledIcon,
} from "@radix-ui/react-icons";

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/util/utils";

const SERVICE_ACCOUNT_WIZARD_URL =
  "https://start.1password.com/developer-tools/infrastructure-secrets/serviceaccount/";
const SERVICE_ACCOUNT_DOCS_URL =
  "https://www.1password.dev/service-accounts/get-started";

type Props = {
  /** Configured orgs are rotating a token, not onboarding — start collapsed. */
  defaultOpen?: boolean;
};

// The wizard path and vault-name restrictions below describe 1Password's own UI,
// which can change without any signal here. Transcribed from
// SERVICE_ACCOUNT_DOCS_URL on 2026-08-25; re-check against that page if someone
// reports the steps no longer match what they see.
const STEPS: Array<{ title: string; detail: ReactNode }> = [
  {
    title: "Open the service account wizard in 1Password",
    detail: (
      <>
        Go to <span className="font-medium">Developer</span> →{" "}
        <span className="font-medium">Directory</span> →{" "}
        <span className="font-medium">Other</span> →{" "}
        <span className="font-medium">Create a Service Account</span>, or use
        the direct link above.
      </>
    ),
  },
  {
    title: "Grant it the vaults holding your login items",
    detail: (
      <>
        A service account cannot reach{" "}
        <span className="font-medium">
          Private, Personal, Employee, or the default Shared
        </span>{" "}
        vault. Move the logins you want Skyvern to use into a separate vault
        first, otherwise they will not show up here.
      </>
    ),
  },
  {
    title: "Give each vault Read Items permission",
    detail: (
      <>
        Skyvern only reads. Vault permissions{" "}
        <span className="font-medium">cannot be changed after creation</span>,
        so select every vault you need now.
      </>
    ),
  },
  {
    title: "Copy the token",
    detail: (
      <>
        1Password shows it <span className="font-medium">only once</span>, and
        it starts with <code className="text-[11px]">ops_</code>. Save it in
        1Password before leaving the wizard.
      </>
    ),
  },
  {
    title: "Paste it below and select Update Token",
    detail: (
      <>
        Your login items then appear in the Credentials page and in every Login
        block&apos;s credential picker.
      </>
    ),
  },
];

function OnePasswordSetupGuide({ defaultOpen = true }: Props) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="rounded-md border border-border bg-muted/40"
    >
      <div className="flex items-center justify-between gap-3 px-3 py-2">
        <CollapsibleTrigger className="flex min-w-0 items-center gap-2 text-left text-sm font-medium">
          <InfoCircledIcon className="size-4 shrink-0 text-muted-foreground" />
          <span className="truncate">
            How to create a service account token
          </span>
          <ChevronDownIcon
            className={cn(
              "size-4 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-180",
            )}
          />
        </CollapsibleTrigger>
        <a
          href={SERVICE_ACCOUNT_WIZARD_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex shrink-0 items-center gap-1.5 text-sm text-blue-600 underline dark:text-blue-400"
        >
          Open 1Password
          <ExternalLinkIcon className="size-3.5" />
        </a>
      </div>

      <CollapsibleContent className="space-y-3 px-3 pb-3">
        <ol className="space-y-2.5">
          {STEPS.map((step, index) => (
            <li key={step.title} className="flex gap-2.5">
              <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-[11px] font-medium text-foreground dark:text-slate-200">
                {index + 1}
              </span>
              <div className="min-w-0 space-y-0.5">
                <div className="text-sm text-foreground dark:text-slate-200">
                  {step.title}
                </div>
                <p className="text-xs leading-5 text-muted-foreground">
                  {step.detail}
                </p>
              </div>
            </li>
          ))}
        </ol>
        <a
          href={SERVICE_ACCOUNT_DOCS_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-xs text-blue-600 underline dark:text-blue-400"
        >
          1Password&apos;s service account documentation
          <ExternalLinkIcon className="size-3" />
        </a>
      </CollapsibleContent>
    </Collapsible>
  );
}

export { OnePasswordSetupGuide };
