import { OverviewCodeBlock } from "./OverviewCodeBlock";
import { OverviewField } from "./OverviewField";

export type RunInputMeta = { label: string; value: string };

type RunInputsSectionProps = {
  // Ordered [key, value] entries for the agent (workflow) inputs this run used.
  parameters: Array<[string, unknown]>;
  // Run-level non-parameter inputs (webhook, proxy, headers, …).
  meta: RunInputMeta[];
};

export function RunInputsSection({ parameters, meta }: RunInputsSectionProps) {
  if (parameters.length === 0 && meta.length === 0) {
    return null;
  }

  const agentInputs = Object.fromEntries(parameters);

  return (
    <div className="flex flex-col gap-5">
      {parameters.length > 0 ? (
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            Agent inputs
          </span>
          <OverviewCodeBlock
            value={JSON.stringify(agentInputs, null, 2)}
            maxHeight="320px"
          />
        </div>
      ) : null}
      {meta.length > 0 ? (
        <div className="flex flex-col gap-3">
          <span className="text-xs font-medium text-muted-foreground">
            Other inputs
          </span>
          {meta.map((entry) => (
            <OverviewField key={entry.label} label={entry.label}>
              <span className="break-all">{entry.value}</span>
            </OverviewField>
          ))}
        </div>
      ) : null}
    </div>
  );
}
