import { create } from "zustand";

type CacheKeyValueStore = {
  cacheKeyValue: string;
  filter: string | null;
  isExplicit: boolean;
  setExplicit: (value: string) => void;
  setFilter: (filter: string | null) => void;
  initialize: (initialValue: string, isExplicit: boolean) => void;
  reset: () => void;
};

const useCacheKeyValueStore = create<CacheKeyValueStore>((set) => {
  return {
    cacheKeyValue: "",
    filter: null,
    isExplicit: false,
    setExplicit: (value: string) => {
      set({ cacheKeyValue: value, isExplicit: true });
    },
    setFilter: (filter: string | null) => {
      set({ filter });
    },
    initialize: (initialValue: string, isExplicit: boolean) => {
      set({ cacheKeyValue: initialValue, isExplicit, filter: null });
    },
    reset: () => {
      set({ cacheKeyValue: "", filter: null, isExplicit: false });
    },
  };
});

export { useCacheKeyValueStore };
