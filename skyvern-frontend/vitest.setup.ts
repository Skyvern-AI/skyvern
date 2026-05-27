type StorageRecord = Record<string, string>;

const createMemoryStorage = (): Storage => {
  let values: StorageRecord = {};

  return {
    get length() {
      return Object.keys(values).length;
    },
    clear() {
      values = {};
    },
    getItem(key: string) {
      return Object.prototype.hasOwnProperty.call(values, key)
        ? (values[key] ?? null)
        : null;
    },
    key(index: number) {
      return Object.keys(values)[index] ?? null;
    },
    removeItem(key: string) {
      delete values[key];
    },
    setItem(key: string, value: string) {
      values[key] = String(value);
    },
  };
};

const localStorageNeedsShim =
  typeof globalThis.localStorage?.clear !== "function" ||
  typeof window.localStorage?.clear !== "function" ||
  typeof globalThis.localStorage?.setItem !== "function" ||
  typeof window.localStorage?.setItem !== "function";

if (localStorageNeedsShim) {
  const storage = createMemoryStorage();

  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: storage,
  });
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
}
