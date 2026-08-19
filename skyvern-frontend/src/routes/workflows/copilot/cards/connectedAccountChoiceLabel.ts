import type { ConnectedAccountChoice } from "../workflowCopilotTypes";

export function connectedAccountChoiceLabel(
  choice: ConnectedAccountChoice,
  choices: ConnectedAccountChoice[],
) {
  const suffix = `Connection …${choice.connection_id.slice(-8)}`;
  const emailAddress = choice.email_address?.trim();
  if (!emailAddress) return suffix;
  const duplicate = choices.some(
    (candidate) =>
      candidate.connection_id !== choice.connection_id &&
      candidate.name === choice.name &&
      candidate.email_address?.trim() === emailAddress,
  );
  return duplicate ? `${emailAddress} · ${suffix}` : emailAddress;
}
