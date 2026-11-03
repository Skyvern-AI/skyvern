import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FeedbackThumbs } from "./FeedbackThumbs";

describe("FeedbackThumbs", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("saves a thumbs down first, then sends the optional reason as a second write", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onRate = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <FeedbackThumbs
        rating={null}
        onRate={onRate}
        prompt="Did this do what you asked?"
      />,
    );
    expect(screen.getByText("Did this do what you asked?")).toBeTruthy();
    expect(screen.queryByLabelText("Feedback reason")).toBeNull();

    fireEvent.click(screen.getByLabelText("Thumbs down"));
    await waitFor(() => expect(onRate).toHaveBeenCalledWith("down", undefined));
    rerender(
      <FeedbackThumbs
        rating="down"
        onRate={onRate}
        prompt="Did this do what you asked?"
      />,
    );

    const reason = await screen.findByLabelText("Feedback reason");
    fireEvent.change(reason, {
      target: { value: "It skipped the second page" },
    });
    fireEvent.click(screen.getByText("Send"));
    await waitFor(() =>
      expect(onRate).toHaveBeenLastCalledWith(
        "down",
        "It skipped the second page",
      ),
    );
    expect(screen.getByText("Thanks, that helps.")).toBeTruthy();
    expect(screen.queryByLabelText("Feedback reason")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.queryByText("Thanks, that helps.")).toBeNull();
    expect(
      screen.getByLabelText("Thumbs down").getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("stays revealed on an older turn while the reason box or a status is showing", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onRate = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <FeedbackThumbs rating={null} onRate={onRate} subtle />,
    );
    const control = screen.getByTestId("feedback-thumbs");
    expect(control.getAttribute("data-subtle")).toBe("true");

    fireEvent.click(screen.getByLabelText("Thumbs down"));
    await waitFor(() => expect(onRate).toHaveBeenCalledWith("down", undefined));
    rerender(<FeedbackThumbs rating="down" onRate={onRate} subtle />);
    await screen.findByLabelText("Feedback reason");
    expect(control.getAttribute("data-subtle")).toBeNull();

    fireEvent.click(screen.getByText("Send"));
    await waitFor(() => expect(onRate).toHaveBeenLastCalledWith("down", ""));
    expect(screen.getByText("Thanks, noted.")).toBeTruthy();
    expect(control.getAttribute("data-subtle")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    // The control starts hiding and the text fades with it rather than unmounting first.
    expect(control.getAttribute("data-subtle")).toBe("true");
    expect(screen.getByText("Thanks, noted.").className).toContain("opacity-0");
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.queryByText("Thanks, noted.")).toBeNull();
  });

  it("keeps the old state and asks to retry when the save fails", async () => {
    const onRate = vi.fn().mockRejectedValue(new Error("offline"));
    render(<FeedbackThumbs rating={null} onRate={onRate} />);
    fireEvent.click(screen.getByLabelText("Thumbs up"));
    expect(await screen.findByText("Couldn't save. Try again.")).toBeTruthy();
    expect(
      screen.getByLabelText("Thumbs up").getAttribute("aria-pressed"),
    ).toBe("false");
  });
});
