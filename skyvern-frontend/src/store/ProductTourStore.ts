import { create } from "zustand";

type ProductTourStore = {
  requestedAt: number | null;
  requestTour: () => void;
  clearRequest: () => void;
};

const useProductTourStore = create<ProductTourStore>((set) => ({
  requestedAt: null,
  requestTour: () => set({ requestedAt: Date.now() }),
  clearRequest: () => set({ requestedAt: null }),
}));

export { useProductTourStore };
