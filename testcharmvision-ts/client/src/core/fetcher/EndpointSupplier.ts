import type { EndpointMetadata } from "./EndpointMetadata.js";
import type { Supplier } from "./Supplier.js";

type EndpointSupplierFn<T> = (arg: { endpointMetadata: EndpointMetadata }) => T | Promise<T>;
export type EndpointSupplier<T> = Supplier<T> | EndpointSupplierFn<T>;
export const EndpointSupplier = {
    get: async <T>(supplier: EndpointSupplier<T>, arg: { endpointMetadata: EndpointMetadata }): Promise<T> => {
        if (typeof supplier === "function") {
            return (supplier as EndpointSupplierFn<T>)(arg);
        } else {
            return supplier;
        }
    },
};
