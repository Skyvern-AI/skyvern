import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import {
  ACK_ROTATE_INTERVAL_MS,
  COPILOT_ACK_LINES,
  InstantAckPlaceholder,
} from "./NarrativeView";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("renders and rotates the instant acknowledgement", () => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0); // deterministic start = line 0
  render(<InstantAckPlaceholder />);

  expect(screen.getByRole("status")).toBeTruthy();
  expect(screen.getByText(COPILOT_ACK_LINES[0])).toBeTruthy();

  act(() => vi.advanceTimersByTime(ACK_ROTATE_INTERVAL_MS));

  expect(screen.getByText(COPILOT_ACK_LINES[1])).toBeTruthy();
  expect(screen.queryByText(COPILOT_ACK_LINES[0])).toBeNull();
});
