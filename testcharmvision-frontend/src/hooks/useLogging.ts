import { LoggingContext } from "@/store/LoggingContext";
import { useContext } from "react";

function useLogging() {
  const getLogging = useContext(LoggingContext);
  return getLogging();
}

export { useLogging };
