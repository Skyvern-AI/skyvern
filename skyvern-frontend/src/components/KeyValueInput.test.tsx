// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render as rtlRender,
  screen,
} from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { KeyValueInput, type KeyValueInputProps } from "./KeyValueInput";

// Both app entrypoints mount under StrictMode, which double-invokes render and
// state updaters — the conditions these assertions have to hold under.
function render(props: KeyValueInputProps) {
  return rtlRender(
    <StrictMode>
      <KeyValueInput {...props} />
    </StrictMode>,
  );
}

const toastMock = vi.hoisted(() => vi.fn());
vi.mock("./ui/use-toast", () => ({ toast: toastMock }));

afterEach(() => {
  cleanup();
  toastMock.mockReset();
});

function pairIds(): string[] {
  return Array.from(document.querySelectorAll("[data-pair-id]")).map(
    (el) => el.getAttribute("data-pair-id") ?? "",
  );
}

function blurOutside() {
  fireEvent.blur(screen.getAllByPlaceholderText("Header")[0]!, {
    relatedTarget: null,
  });
}

describe("KeyValueInput", () => {
  // Mounting used to hand the parent a re-serialized value ("{}" became ""),
  // so merely opening a settings panel wrote to the form it lives in.
  test.each([
    ["an empty object", {} as Record<string, string>],
    ["an empty JSON string", "{}"],
    ["null", null],
    ["a populated object", { "X-Trace": "1" }],
    ["a populated JSON string", '{"X-Trace":"1"}'],
  ])("mounting with %s does not notify the parent", (_label, value) => {
    const onChange = vi.fn();
    render({ value, onChange });
    expect(onChange).not.toHaveBeenCalled();
  });

  test("a user edit still propagates, matching the value's shape", () => {
    const onChange = vi.fn();
    render({ value: '{"X-Trace":"1"}', onChange });

    fireEvent.change(screen.getByPlaceholderText("Value"), {
      target: { value: "2" },
    });

    expect(onChange).toHaveBeenCalledWith('{"X-Trace":"2"}');
  });

  test("an object-shaped value emits an object", () => {
    const onChange = vi.fn();
    render({ value: { "X-Trace": "1" }, onChange });

    fireEvent.change(screen.getByPlaceholderText("Value"), {
      target: { value: "2" },
    });

    expect(onChange).toHaveBeenCalledWith({ "X-Trace": "2" });
  });

  // Rebuilding every row (new nanoid = new React key) on focus-out remounted
  // the inputs, dropping the caret and re-firing this same handler for the
  // input being removed.
  test("leaving the component keeps existing rows mounted", () => {
    const onChange = vi.fn();
    render({ value: '{"X-Trace":"1"}', onChange });
    const before = pairIds();

    blurOutside();

    expect(pairIds()).toEqual(before);
    expect(onChange).not.toHaveBeenCalled();
  });

  test("leaving the component resolves a duplicate key, last one winning", () => {
    const onChange = vi.fn();
    render({ value: '{"X-Trace":"1"}', onChange });

    fireEvent.click(screen.getByRole("button", { name: /Add/ }));
    const keys = screen.getAllByPlaceholderText("Header");
    fireEvent.change(keys[1]!, { target: { value: "X-Trace" } });
    fireEvent.change(screen.getAllByPlaceholderText("Value")[1]!, {
      target: { value: "2" },
    });
    onChange.mockReset();

    blurOutside();

    expect(screen.getAllByPlaceholderText("Header")).toHaveLength(1);
    expect(onChange).toHaveBeenCalledWith('{"X-Trace":"2"}');
    expect(toastMock).toHaveBeenCalledTimes(1);
  });

  // Walking the rows in reverse compared each loser against the previous row
  // rather than the survivor, so the first toast named a value the user never
  // ends up with.
  test("every duplicate toast names the value that survives", () => {
    const onChange = vi.fn();
    render({ value: '{"X-Trace":"1"}', onChange });

    for (const suffix of ["2", "3"]) {
      fireEvent.click(screen.getByRole("button", { name: /Add/ }));
      const rows = screen.getAllByPlaceholderText("Header");
      fireEvent.change(rows[rows.length - 1]!, {
        target: { value: "X-Trace" },
      });
      const values = screen.getAllByPlaceholderText("Value");
      fireEvent.change(values[values.length - 1]!, {
        target: { value: suffix },
      });
    }

    blurOutside();

    expect(onChange).toHaveBeenCalledWith('{"X-Trace":"3"}');
    expect(toastMock).toHaveBeenCalledTimes(2);
    for (const call of toastMock.mock.calls) {
      expect(call[0].description).toContain("to '3'");
    }
  });

  test("leaving the component drops a row the user never named", () => {
    const onChange = vi.fn();
    render({ value: '{"X-Trace":"1"}', onChange });

    fireEvent.click(screen.getByRole("button", { name: /Add/ }));
    expect(screen.getAllByPlaceholderText("Header")).toHaveLength(2);
    onChange.mockReset();

    blurOutside();

    expect(screen.getAllByPlaceholderText("Header")).toHaveLength(1);
    expect(onChange).not.toHaveBeenCalled();
  });
});
