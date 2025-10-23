import type { WithRawResponse } from "./RawResponse.mjs";
/**
 * A promise that returns the parsed response and lets you retrieve the raw response too.
 */
export declare class HttpResponsePromise<T> extends Promise<T> {
    private innerPromise;
    private unwrappedPromise;
    private constructor();
    /**
     * Creates an `HttpResponsePromise` from a function that returns a promise.
     *
     * @param fn - A function that returns a promise resolving to a `WithRawResponse` object.
     * @param args - Arguments to pass to the function.
     * @returns An `HttpResponsePromise` instance.
     */
    static fromFunction<F extends (...args: never[]) => Promise<WithRawResponse<T>>, T>(fn: F, ...args: Parameters<F>): HttpResponsePromise<T>;
    /**
     * Creates a function that returns an `HttpResponsePromise` from a function that returns a promise.
     *
     * @param fn - A function that returns a promise resolving to a `WithRawResponse` object.
     * @returns A function that returns an `HttpResponsePromise` instance.
     */
    static interceptFunction<F extends (...args: never[]) => Promise<WithRawResponse<T>>, T = Awaited<ReturnType<F>>["data"]>(fn: F): (...args: Parameters<F>) => HttpResponsePromise<T>;
    /**
     * Creates an `HttpResponsePromise` from an existing promise.
     *
     * @param promise - A promise resolving to a `WithRawResponse` object.
     * @returns An `HttpResponsePromise` instance.
     */
    static fromPromise<T>(promise: Promise<WithRawResponse<T>>): HttpResponsePromise<T>;
    /**
     * Creates an `HttpResponsePromise` from an executor function.
     *
     * @param executor - A function that takes resolve and reject callbacks to create a promise.
     * @returns An `HttpResponsePromise` instance.
     */
    static fromExecutor<T>(executor: (resolve: (value: WithRawResponse<T>) => void, reject: (reason?: unknown) => void) => void): HttpResponsePromise<T>;
    /**
     * Creates an `HttpResponsePromise` from a resolved result.
     *
     * @param result - A `WithRawResponse` object to resolve immediately.
     * @returns An `HttpResponsePromise` instance.
     */
    static fromResult<T>(result: WithRawResponse<T>): HttpResponsePromise<T>;
    private unwrap;
    /** @inheritdoc */
    then<TResult1 = T, TResult2 = never>(onfulfilled?: ((value: T) => TResult1 | PromiseLike<TResult1>) | null, onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null): Promise<TResult1 | TResult2>;
    /** @inheritdoc */
    catch<TResult = never>(onrejected?: ((reason: unknown) => TResult | PromiseLike<TResult>) | null): Promise<T | TResult>;
    /** @inheritdoc */
    finally(onfinally?: (() => void) | null): Promise<T>;
    /**
     * Retrieves the data and raw response.
     *
     * @returns A promise resolving to a `WithRawResponse` object.
     */
    withRawResponse(): Promise<WithRawResponse<T>>;
}
