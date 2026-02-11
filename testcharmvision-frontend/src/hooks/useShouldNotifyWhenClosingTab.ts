import { useEffect } from "react";

function useShouldNotifyWhenClosingTab(shouldNotify: boolean) {
  useEffect(() => {
    function f(event: BeforeUnloadEvent) {
      // this function is here to have a stable reference only
      if (!shouldNotify) {
        return undefined;
      }
      // Recommended
      event.preventDefault();
      // Included for legacy support, e.g. Chrome/Edge < 119
      // refer to https://developer.mozilla.org/en-US/docs/Web/API/Window/beforeunload_event
      event.returnValue = true;
    }

    window.addEventListener("beforeunload", f);

    return () => {
      window.removeEventListener("beforeunload", f);
    };
  }, [shouldNotify]);
}

export { useShouldNotifyWhenClosingTab };
