import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { useDismissedFlag } from "./useDismissedFlag";

afterEach(() => window.localStorage.clear());

it("remembers a dismissal across mounts and follows key changes", () => {
  const hook = renderHook(({ key }) => useDismissedFlag(key), {
    initialProps: { key: "org-a" },
  });
  expect(hook.result.current[0]).toBe(false);
  act(() => hook.result.current[1]());
  expect(hook.result.current[0]).toBe(true);
  hook.rerender({ key: "org-b" });
  expect(hook.result.current[0]).toBe(false);
  hook.rerender({ key: "org-a" });
  expect(hook.result.current[0]).toBe(true);
  hook.unmount();
  expect(renderHook(() => useDismissedFlag("org-a")).result.current[0]).toBe(
    true,
  );
});
