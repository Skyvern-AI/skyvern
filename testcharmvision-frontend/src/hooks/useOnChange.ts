import { useEffect, useRef } from "react";

function useOnChange<T>(
  value: T,
  callback: (newValue: T, prevValue: T | undefined) => void,
) {
  const prevValue = useRef<T>(value);

  useEffect(() => {
    if (prevValue.current !== undefined) {
      callback(value, prevValue.current);
    }
    prevValue.current = value;
  }, [value, callback]);
}

export { useOnChange };
