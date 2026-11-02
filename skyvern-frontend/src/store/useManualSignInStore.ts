import { useEffect, useRef } from "react";
import { create } from "zustand";

type ManualSignInStore = {
  // The live browser the user is signing in to from a Copilot credential card, if any.
  browserSessionId: string | null;
  setBrowserSessionId: (browserSessionId: string | null) => void;
};

export const useManualSignInStore = create<ManualSignInStore>((set) => ({
  browserSessionId: null,
  setBrowserSessionId: (browserSessionId) => set({ browserSessionId }),
}));

// Hands the stream to the user while they sign in to this browser, and back when they finish.
export function useManualSignInControl(
  browserSessionId: string | undefined,
  setUserIsControlling: (controlling: boolean) => void,
) {
  const signingIn = useManualSignInStore(
    (state) =>
      browserSessionId !== undefined &&
      state.browserSessionId === browserSessionId,
  );
  const wasSigningIn = useRef(false);
  useEffect(() => {
    if (signingIn !== wasSigningIn.current) {
      setUserIsControlling(signingIn);
    }
    wasSigningIn.current = signingIn;
  }, [signingIn, setUserIsControlling]);
}

export function useDemonstrationControl(
  recording: boolean,
  controllable: boolean,
  setUserIsControlling: (controlling: boolean) => void,
  connectionReady = true,
) {
  const holdingControl = recording && controllable;
  const wasHoldingControl = useRef(false);
  useEffect(() => {
    if (holdingControl) {
      setUserIsControlling(true);
    } else if (wasHoldingControl.current) {
      setUserIsControlling(false);
    }
    wasHoldingControl.current = holdingControl;
  }, [connectionReady, holdingControl, setUserIsControlling]);
}
