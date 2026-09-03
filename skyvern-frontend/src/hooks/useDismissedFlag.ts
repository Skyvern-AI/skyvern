import { useState } from "react";

// Per-browser dismissal for surfaces the server cannot hide (it rejects
// visibility changes on completed onboarding progress).
function readDismissedFlag(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function setDismissedFlag(key: string) {
  try {
    window.localStorage.setItem(key, "1");
  } catch {
    // Storage can be unavailable in embedded contexts; hide for the session.
  }
}

function useDismissedFlag(key: string): [boolean, () => void] {
  const [state, setState] = useState(() => ({
    key,
    dismissed: readDismissedFlag(key),
  }));
  if (state.key !== key) {
    setState({ key, dismissed: readDismissedFlag(key) });
  }
  return [
    state.key === key && state.dismissed,
    () => {
      setDismissedFlag(key);
      setState({ key, dismissed: true });
    },
  ];
}

export { setDismissedFlag, useDismissedFlag };
