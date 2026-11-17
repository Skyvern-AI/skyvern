import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { hydrateNarrativeFromPayload } from "../narrativeState";
import { TriggerCreatedCard } from "./TriggerCreatedCard";

describe("TriggerCreatedCard", () => {
  it("shows a persisted trigger as off with its filters and opens it", () => {
    const [trigger] = hydrateNarrativeFromPayload({
      turnId: "turn_1",
      createdTriggers: [
        {
          eventTriggerId: "etr_example",
          sourceKind: "example_source",
          filters: [
            { label: "Label", value: "INBOX" },
            { label: "From contains", value: "billing@example.com" },
          ],
        },
        { sourceKind: "http_signed" },
      ],
    })!.createdTriggers;
    const onOpen = vi.fn();
    render(
      <TriggerCreatedCard trigger={trigger!} onOpen={onOpen} enabled={false} />,
    );

    expect(screen.getByText("Event trigger created")).toBeTruthy();
    expect(screen.getByText("Off")).toBeTruthy();
    expect(screen.getByText("From contains")).toBeTruthy();
    expect(screen.getByText("billing@example.com")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open in Triggers" }));
    expect(onOpen).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Turn on" })).toBeNull();
  });
});
