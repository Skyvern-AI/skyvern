import { createContext } from "react";

type LogFn = (message: string, data?: Record<string, unknown>) => void;

interface Logging {
  info: LogFn;
  warn: LogFn;
  error: LogFn;
}

// make this a stub of LogFn that does nothing

// eslint-disable-next-line @typescript-eslint/no-unused-vars
const noop: LogFn = (..._: Parameters<LogFn>) => {};

const stub: Logging = {
  info: noop,
  warn: noop,
  error: noop,
};

type GetLogging = () => Logging;

const LoggingContext = createContext<GetLogging>(() => stub);

export { LoggingContext, stub as loggingStub };
