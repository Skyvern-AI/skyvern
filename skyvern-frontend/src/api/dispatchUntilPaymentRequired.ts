import { isPaymentRequiredError } from "./paymentRequired";

type DispatchOutcome<TResult> = {
  // Indexed to match the input; entries for undispatched items stay undefined.
  results: Array<PromiseSettledResult<TResult> | undefined>;
  paymentRequired: boolean;
  dispatchedCount: number;
  skippedCount: number;
};

// Fanning every item out at once leaves nothing to stop: by the time the first
// 402 lands, the whole batch is already on the wire. Dispatching through a
// small pool keeps a tail of undispatched items for the latch to skip.
const DEFAULT_CONCURRENCY = 6;

/**
 * Dispatch `items` through a bounded pool, stopping as soon as one of them
 * fails with 402. Undispatched items are counted in `skippedCount` rather than
 * sent. Every dispatched promise is awaited inside the pool, so a rejection is
 * never momentarily unhandled — an unhandled rejection is reported to Datadog
 * by `forwardErrorsToLogs` even when it is an expected paywall response.
 */
async function dispatchUntilPaymentRequired<TItem, TResult>(
  items: ReadonlyArray<TItem>,
  dispatch: (item: TItem, index: number) => Promise<TResult>,
  concurrency: number = DEFAULT_CONCURRENCY,
): Promise<DispatchOutcome<TResult>> {
  const results: Array<PromiseSettledResult<TResult> | undefined> = Array.from(
    { length: items.length },
    () => undefined,
  );
  const pending = items.entries();
  let dispatchedCount = 0;
  let paymentRequired = false;

  const worker = async (): Promise<void> => {
    while (!paymentRequired) {
      const next = pending.next();
      if (next.done) {
        return;
      }
      const [index, item] = next.value;
      dispatchedCount += 1;
      try {
        results[index] = {
          status: "fulfilled",
          value: await dispatch(item, index),
        };
      } catch (error) {
        if (isPaymentRequiredError(error)) {
          paymentRequired = true;
        }
        results[index] = { status: "rejected", reason: error };
      }
    }
  };

  await Promise.all(
    Array.from({ length: Math.min(concurrency, items.length) }, () => worker()),
  );

  return {
    results,
    paymentRequired,
    dispatchedCount,
    skippedCount: items.length - dispatchedCount,
  };
}

export { dispatchUntilPaymentRequired };
export type { DispatchOutcome };
