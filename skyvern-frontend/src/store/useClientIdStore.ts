import { create } from "zustand";

type ClientIdStore = {
  clientId: string;
};

const initialClientId = crypto.randomUUID();

export const useClientIdStore = create<ClientIdStore>(() => ({
  clientId: initialClientId,
}));
