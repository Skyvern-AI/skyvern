import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
} from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { DocGettingStarted, DocTip } from "./DocStarter";
import { docTipIndex } from "./docStarterTips";
import { useDocStarter } from "./useDocStarter";

const fresh = {
  steps: 0,
  usedReference: false,
  nested: false,
  hasContainer: false,
  problems: 0,
  ran: false,
};

describe("doc starter", () => {
  test("a new agent's checklist counts what the agent already does", () => {
    const { rerender } = render(
      <DocGettingStarted progress={fresh} onHide={() => {}} />,
    );
    expect(screen.getByText("0 of 4")).toBeTruthy();
    rerender(
      <DocGettingStarted
        progress={{ ...fresh, steps: 2, nested: true }}
        onHide={() => {}}
      />,
    );
    expect(screen.getByText("2 of 4")).toBeTruthy();
  });

  test("an item stays ticked after the agent stops satisfying it", () => {
    render(
      <DocGettingStarted
        progress={{ ...fresh, steps: 1 }}
        kept={["nest"]}
        onHide={() => {}}
      />,
    );
    expect(screen.getByText("2 of 4")).toBeTruthy();
  });

  test("tips skip the ones already seen and wait for what they talk about", () => {
    render(
      <DocTip
        progress={{ ...fresh, steps: 1 }}
        seen={["slash"]}
        onSeen={() => {}}
      />,
    );
    expect(screen.getByText(/Backspace at the start/)).toBeTruthy();
  });
});

describe("doc starter placement", () => {
  test("a narrow pane shows a pill that opens the card", () => {
    render(<DocGettingStarted progress={fresh} onHide={() => {}} compact />);
    expect(screen.queryByRole("complementary")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Getting started/ }));
    expect(screen.getByText("0 of 4")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Collapse checklist" }));
    expect(screen.queryByText("0 of 4")).toBeNull();
  });

  test("the card folds to its pill while a toast or run result needs the corner, and never moves up or opens", () => {
    const { container, rerender } = render(
      <DocGettingStarted progress={fresh} onHide={() => {}} />,
    );
    expect(container.querySelector("aside")?.className).toContain("bottom-4");
    rerender(<DocGettingStarted progress={fresh} onHide={() => {}} folded />);
    expect(container.querySelector("aside")).toBeNull();
    const pill = screen.getByRole("button", { name: /Getting started/ });
    expect(pill.className).toContain("bottom-4");
    expect(pill.className).not.toContain("bottom-36");
    expect((pill as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(pill);
    expect(container.querySelector("aside")).toBeNull();
    rerender(<DocGettingStarted progress={fresh} onHide={() => {}} compact />);
    expect(container.querySelector("aside")).toBeNull();
  });

  test("a tip is pending until seen, so the checklist can wait for it", () => {
    expect(docTipIndex(fresh, [])).toBe(0);
    expect(docTipIndex(fresh, ["slash"])).toBe(-1);
  });
});

describe("doc starter dismissal", () => {
  test("a checklist hidden for an agent stays hidden when its empty doc opens again", () => {
    localStorage.clear();
    const first = renderHook(() => useDocStarter("wpid_a", true));
    expect(first.result.current.active).toBe(true);
    act(() => first.result.current.hide());
    expect(first.result.current.active).toBe(false);
    first.unmount();
    const again = renderHook(() => useDocStarter("wpid_a", true));
    expect(again.result.current.active).toBe(false);
  });

  test("items ticked once are remembered for the agent", () => {
    localStorage.clear();
    const first = renderHook(() => useDocStarter("wpid_b", true));
    act(() => first.result.current.markDone(["nest"]));
    first.unmount();
    const again = renderHook(() => useDocStarter("wpid_b", true));
    expect(again.result.current.done).toEqual(["nest"]);
  });
});
