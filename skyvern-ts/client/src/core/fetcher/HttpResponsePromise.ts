import type { WithRawResponse } from "./RawResponse.js";

/**
 * A promise that returns the parsed response and lets you retrieve the raw response too.
 */
export class HttpResponsePromise<T> extends Promise<T> {
    private innerPromise: Promise<WithRawResponse<T>>;
    private unwrappedPromise: Promise<T> | undefined;

    private constructor(promise: Promise<WithRawResponse<T>>) {
        // Initialize with a no-op to avoid premature parsing
        super((resolve) => {
            resolve(undefined as unknown as T);
        });
        this.innerPromise = promise;
    }

    /**
     * Creates an `HttpResponsePromise` from a function that returns a promise.
     *
     * @param fn - A function that returns a promise resolving to a `WithRawResponse` object.
     * @param args - Arguments to pass to the function.
     * @returns An `HttpResponsePromise` instance.
     */
    public static fromFunction<F extends (...args: never[]) => Promise<WithRawResponse<T>>, T>(
        fn: F,
        ...args: Parameters<F>
    ): HttpResponsePromise<T> {
        return new HttpResponsePromise<T>(fn(...args));
    }

    /**
     * Creates a function that returns an `HttpResponsePromise` from a function that returns a promise.
     *
     * @param fn - A function that returns a promise resolving to a `WithRawResponse` object.
     * @returns A function that returns an `HttpResponsePromise` instance.
     */
    public static interceptFunction<
        F extends (...args: never[]) => Promise<WithRawResponse<T>>,
        T = Awaited<ReturnType<F>>["data"],
    >(fn: F): (...args: Parameters<F>) => HttpResponsePromise<T> {
        return (...args: Parameters<F>): HttpResponsePromise<T> => {
            return HttpResponsePromise.fromPromise<T>(fn(...args));
        };
    }

    /**
     * Creates an `HttpResponsePromise` from an existing promise.
     *
     * @param promise - A promise resolving to a `WithRawResponse` object.
     * @returns An `HttpResponsePromise` instance.
     */
    public static fromPromise<T>(promise: Promise<WithRawResponse<T>>): HttpResponsePromise<T> {
        return new HttpResponsePromise<T>(promise);
    }

    /**
     * Creates an `HttpResponsePromise` from an executor function.
     *
     * @param executor - A function that takes resolve and reject callbacks to create a promise.
     * @returns An `HttpResponsePromise` instance.
     */
    public static fromExecutor<T>(
        executor: (resolve: (value: WithRawResponse<T>) => void, reject: (reason?: unknown) => void) => void,
    ): HttpResponsePromise<T> {
        const promise = new Promise<WithRawResponse<T>>(executor);
        return new HttpResponsePromise<T>(promise);
    }

    /**
     * Creates an `HttpResponsePromise` from a resolved result.
     *
     * @param result - A `WithRawResponse` object to resolve immediately.
     * @returns An `HttpResponsePromise` instance.
     */
    public static fromResult<T>(result: WithRawResponse<T>): HttpResponsePromise<T> {
        const promise = Promise.resolve(result);
        return new HttpResponsePromise<T>(promise);
    }

    private unwrap(): Promise<T> {
        if (!this.unwrappedPromise) {
            this.unwrappedPromise = this.innerPromise.then(({ data }) => data);
        }
        return this.unwrappedPromise;
    }

    /** @inheritdoc */
    public override then<TResult1 = T, TResult2 = never>(
        onfulfilled?: ((value: T) => TResult1 | PromiseLike<TResult1>) | null,
        onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
    ): Promise<TResult1 | TResult2> {
        return this.unwrap().then(onfulfilled, onrejected);
    }

    /** @inheritdoc */
    public override catch<TResult = never>(
        onrejected?: ((reason: unknown) => TResult | PromiseLike<TResult>) | null,
    ): Promise<T | TResult> {
        return this.unwrap().catch(onrejected);
    }

    /** @inheritdoc */
    public override finally(onfinally?: (() => void) | null): Promise<T> {
        return this.unwrap().finally(onfinally);
    }

    /**
     * Retrieves the data and raw response.
     *
     * @returns A promise resolving to a `WithRawResponse` object.
     */
    public async withRawResponse(): Promise<WithRawResponse<T>> {
        return await this.innerPromise;
    }
}
