export type Supplier<T> = T | Promise<T> | (() => T | Promise<T>);

export const Supplier = {
    get: async <T>(supplier: Supplier<T>): Promise<T> => {
        if (typeof supplier === "function") {
            return (supplier as () => T)();
        } else {
            return supplier;
        }
    },
};
