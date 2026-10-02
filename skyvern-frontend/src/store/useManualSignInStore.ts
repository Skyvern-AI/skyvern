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
