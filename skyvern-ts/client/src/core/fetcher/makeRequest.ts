import { anySignal, getTimeoutSignal } from "./signals.js";

export const makeRequest = async (
    fetchFn: (url: string, init: RequestInit) => Promise<Response>,
    url: string,
    method: string,
    headers: Record<string, string>,
    requestBody: BodyInit | undefined,
    timeoutMs?: number,
    abortSignal?: AbortSignal,
    withCredentials?: boolean,
    duplex?: "half",
): Promise<Response> => {
    const signals: AbortSignal[] = [];

    // Add timeout signal
    let timeoutAbortId: NodeJS.Timeout | undefined;
    if (timeoutMs != null) {
        const { signal, abortId } = getTimeoutSignal(timeoutMs);
        timeoutAbortId = abortId;
        signals.push(signal);
    }

    // Add arbitrary signal
    if (abortSignal != null) {
        signals.push(abortSignal);
    }
    const newSignals = anySignal(signals);
    const response = await fetchFn(url, {
        method: method,
        headers,
        body: requestBody,
        signal: newSignals,
        credentials: withCredentials ? "include" : undefined,
        // @ts-ignore
        duplex,
    });

    if (timeoutAbortId != null) {
        clearTimeout(timeoutAbortId);
    }

    return response;
};
