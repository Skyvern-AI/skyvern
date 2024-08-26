import { useEffect } from "react";

function useMountEffect(callback: () => void) {
  return useEffect(() => {
    callback();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

export { useMountEffect };
