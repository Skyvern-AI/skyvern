import { create } from "zustand";

type SettingsStore = {
  environment: string;
  organization: string;
  setEnvironment: (environment: string) => void;
  setOrganization: (organization: string) => void;
};

const useSettingsStore = create<SettingsStore>((set) => {
  return {
    environment: "local",
    organization: "skyvern",
    setEnvironment: (environment: string) => set({ environment }),
    setOrganization: (organization: string) => set({ organization }),
  };
});

export { useSettingsStore };
