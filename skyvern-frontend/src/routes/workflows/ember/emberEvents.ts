// PostHog event names keep their pre-rename bytes so insights and funnels stay continuous.
export const EMBER_EVENTS = {
  messageSent: "copilot.message.sent",
  turnFailed: "copilot.turn.failed",
  turnCancelled: "copilot.turn.cancelled",
  proposalAccepted: "copilot.proposal.accepted",
  proposalRejected: "copilot.proposal.rejected",
  autoAcceptToggled: "copilot.auto_accept.toggled",
  paneOpened: "copilot.pane.opened",
  discoverStarted: "copilot.discover.started",
} as const;
