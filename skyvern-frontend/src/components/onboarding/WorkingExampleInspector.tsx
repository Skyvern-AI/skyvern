import { ReloadIcon } from "@radix-ui/react-icons";

import { Button } from "@/components/ui/button";
import { onboardingExamplePresentation } from "@/routes/discover/onboardingExample";

type WorkingExampleInspectorProps = {
  isPending: boolean;
  onMakeCopy: () => void;
};

function WorkingExampleInspector({
  isPending,
  onMakeCopy,
}: WorkingExampleInspectorProps) {
  const { title, provenance, structure, playback, result } =
    onboardingExamplePresentation;

  return (
    <section
      aria-labelledby="working-example-heading"
      className="overflow-hidden rounded-lg border border-border bg-background"
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-5 py-4 sm:px-6">
        <h2
          id="working-example-heading"
          tabIndex={-1}
          className="scroll-mt-24 text-base font-semibold"
        >
          {title}
        </h2>
        <p className="shrink-0 text-xs font-medium text-muted-foreground">
          {provenance}
        </p>
      </header>
      <div className="grid gap-5 p-5 sm:p-6 lg:grid-cols-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">How the agent is built</h3>
          <ol className="mt-3 list-decimal space-y-3 pl-4 marker:text-muted-foreground">
            {structure.map((step) => (
              <li key={step.title} className="break-words pl-1">
                <p className="text-sm font-medium">{step.title}</p>
                <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
                  {step.detail}
                </p>
              </li>
            ))}
          </ol>
        </div>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">What the agent did</h3>
          <ol className="mt-3 list-decimal space-y-2 pl-4 marker:text-muted-foreground">
            {playback.map((detail) => (
              <li
                key={detail}
                className="break-words pl-1 text-xs leading-5 text-muted-foreground"
              >
                {detail}
              </li>
            ))}
          </ol>
        </div>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold">{result.title}</h3>
          <dl className="mt-3 divide-y divide-border rounded-md border border-border">
            {result.fields.map(({ label, value }) => (
              <div
                key={label}
                className="grid gap-1 px-3 py-2.5 sm:grid-cols-[7rem_1fr] sm:gap-3"
              >
                <dt className="break-words text-xs font-medium text-muted-foreground">
                  {label}
                </dt>
                <dd className="min-w-0 break-words text-xs leading-5">
                  {value}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
      <footer className="flex justify-end border-t border-border px-5 py-4 sm:px-6">
        <Button
          type="button"
          variant="outline"
          className="h-11 w-full touch-manipulation sm:w-auto"
          disabled={isPending}
          aria-busy={isPending}
          onClick={onMakeCopy}
        >
          {isPending && (
            <ReloadIcon
              aria-hidden="true"
              className="mr-2 h-4 w-4 motion-safe:animate-spin motion-reduce:animate-none"
            />
          )}
          Make a copy
        </Button>
      </footer>
    </section>
  );
}

export { WorkingExampleInspector };
