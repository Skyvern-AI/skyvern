export interface Logger {
    debug(message: string, context?: Record<string, unknown>): void;
    info(message: string, context?: Record<string, unknown>): void;
    warn(message: string, context?: Record<string, unknown>): void;
    error(message: string, context?: Record<string, unknown>): void;
}

function formatLogMessage(message: string, context?: Record<string, unknown>): string {
    const prefix = "[Skyvern]";

    if (!context || Object.keys(context).length === 0) {
        return `${prefix} ${message}`;
    }

    const contextParts = Object.entries(context)
        .filter(([, value]) => value !== null && value !== undefined)
        .map(([key, value]) => {
            if (typeof value === "object") {
                return `${key}=${JSON.stringify(value)}`;
            }
            return `${key}=${value}`;
        })
        .join(" ");

    if (contextParts.length === 0) {
        return `${prefix} ${message}`;
    }

    return `${prefix} ${message}\t${contextParts}`;
}

const defaultLogger: Logger = {
    debug: (message: string, context?: Record<string, unknown>) => {
        console.debug(formatLogMessage(message, context));
    },
    info: (message: string, context?: Record<string, unknown>) => {
        console.info(formatLogMessage(message, context));
    },
    warn: (message: string, context?: Record<string, unknown>) => {
        console.warn(formatLogMessage(message, context));
    },
    error: (message: string, context?: Record<string, unknown>) => {
        console.error(formatLogMessage(message, context));
    },
};

let currentLogger: Logger = defaultLogger;

export function setLogger(logger: Logger): void {
    currentLogger = logger;
}

class StructuredLogger {
    debug(message: string, context?: Record<string, unknown>): void {
        currentLogger.debug(message, context);
    }

    info(message: string, context?: Record<string, unknown>): void {
        currentLogger.info(message, context);
    }

    warn(message: string, context?: Record<string, unknown>): void {
        currentLogger.warn(message, context);
    }

    error(message: string, context?: Record<string, unknown>): void {
        currentLogger.error(message, context);
    }
}

export const LOG: StructuredLogger = new StructuredLogger();
