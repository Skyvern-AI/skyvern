"use strict";
var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.HttpResponsePromise = void 0;
/**
 * A promise that returns the parsed response and lets you retrieve the raw response too.
 */
class HttpResponsePromise extends Promise {
    constructor(promise) {
        // Initialize with a no-op to avoid premature parsing
        super((resolve) => {
            resolve(undefined);
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
    static fromFunction(fn, ...args) {
        return new HttpResponsePromise(fn(...args));
    }
    /**
     * Creates a function that returns an `HttpResponsePromise` from a function that returns a promise.
     *
     * @param fn - A function that returns a promise resolving to a `WithRawResponse` object.
     * @returns A function that returns an `HttpResponsePromise` instance.
     */
    static interceptFunction(fn) {
        return (...args) => {
            return HttpResponsePromise.fromPromise(fn(...args));
        };
    }
    /**
     * Creates an `HttpResponsePromise` from an existing promise.
     *
     * @param promise - A promise resolving to a `WithRawResponse` object.
     * @returns An `HttpResponsePromise` instance.
     */
    static fromPromise(promise) {
        return new HttpResponsePromise(promise);
    }
    /**
     * Creates an `HttpResponsePromise` from an executor function.
     *
     * @param executor - A function that takes resolve and reject callbacks to create a promise.
     * @returns An `HttpResponsePromise` instance.
     */
    static fromExecutor(executor) {
        const promise = new Promise(executor);
        return new HttpResponsePromise(promise);
    }
    /**
     * Creates an `HttpResponsePromise` from a resolved result.
     *
     * @param result - A `WithRawResponse` object to resolve immediately.
     * @returns An `HttpResponsePromise` instance.
     */
    static fromResult(result) {
        const promise = Promise.resolve(result);
        return new HttpResponsePromise(promise);
    }
    unwrap() {
        if (!this.unwrappedPromise) {
            this.unwrappedPromise = this.innerPromise.then(({ data }) => data);
        }
        return this.unwrappedPromise;
    }
    /** @inheritdoc */
    then(onfulfilled, onrejected) {
        return this.unwrap().then(onfulfilled, onrejected);
    }
    /** @inheritdoc */
    catch(onrejected) {
        return this.unwrap().catch(onrejected);
    }
    /** @inheritdoc */
    finally(onfinally) {
        return this.unwrap().finally(onfinally);
    }
    /**
     * Retrieves the data and raw response.
     *
     * @returns A promise resolving to a `WithRawResponse` object.
     */
    withRawResponse() {
        return __awaiter(this, void 0, void 0, function* () {
            return yield this.innerPromise;
        });
    }
}
exports.HttpResponsePromise = HttpResponsePromise;
