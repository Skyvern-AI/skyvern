import { create } from "zustand";
import { nanoid } from "nanoid";

type ClientIdStore = {
  clientId: string;
};

const generateClientId = (): string => {
  try {
    return crypto.randomUUID();
  } catch (error) {
    // if crypto.randomUUID() fails, use nanoid as a fallback
    return nanoid();
  }
};

const initialClientId = generateClientId();

export const useClientIdStore = create<ClientIdStore>(() => ({
  clientId: initialClientId,
}));
