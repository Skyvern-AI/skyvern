/**
 * A store to hold block outputs for the debugger. Overrides for block outputs,
 * keyed by (wpid, blockLabel), are kept in local storage.
 */

import { create } from "zustand";

interface BlockOutputStore {
  outputs: { [blockLabel: string]: { [k: string]: unknown } };
  useOverrides: { [wpid: string]: { [blockLabel: string]: boolean } };
  // --
  getOverride: (opts: {
    wpid: string | undefined;
    blockLabel: string;
  }) => { [k: string]: unknown } | null;
  getUseOverride: (opts: {
    wpid: string | undefined;
    blockLabel: string;
  }) => boolean;
  getOutputsWithOverrides: (wpid: string | undefined) => {
    [blockLabel: string]: { [k: string]: unknown };
  };
  setOutputs: (outputs: {
    [blockLabel: string]: { [k: string]: unknown };
  }) => void;
  setOverride: (opts: {
    wpid: string | undefined;
    blockLabel: string;
    data: { [k: string]: unknown };
  }) => boolean;
  setUseOverride: (opts: {
    wpid: string | undefined;
    blockLabel: string;
    value: boolean;
  }) => void;
  reset: () => void;
}

const getStorageKey = (wpid: string, blockLabel: string) => {
  return `skyvern.block-output.${wpid}.${blockLabel}`;
};

const getStorageKeyForUse = (wpid: string, blockLabel: string) => {
  return `skyvern.block-output.use.${wpid}.${blockLabel}`;
};

const serialize = (
  blockLabel: string,
  data: { [k: string]: unknown } | boolean,
) => {
  let serialized: string | null = null;

  try {
    serialized = JSON.stringify(data);
  } catch (e) {
    console.error(`Cannot serialize data for ${blockLabel}`, e, data);
  }

  if (serialized === null) {
    return false;
  }

  if (serialized.trim() === "") {
    serialized = "null";
  }

  return serialized;
};

const loadUse = (wpid: string, blockLabel: string) => {
  const key = getStorageKeyForUse(wpid, blockLabel);
  const serialized = localStorage.getItem(key);

  if (!serialized) {
    return false;
  }

  try {
    return Boolean(JSON.parse(serialized));
  } catch (e) {
    console.error(`Cannot deserialize use override for ${blockLabel}`, e);
    return null;
  }
};

const load = (wpid: string, blockLabel: string) => {
  const key = getStorageKey(wpid, blockLabel);
  const serialized = localStorage.getItem(key);

  if (!serialized) {
    return null;
  }

  try {
    return JSON.parse(serialized) as { [k: string]: unknown };
  } catch (e) {
    console.error(
      `Cannot deserialize block output override for ${blockLabel}`,
      e,
    );
    return null;
  }
};

const store = (
  wpid: string,
  blockLabel: string,
  data: { [k: string]: unknown },
) => {
  const key = getStorageKey(wpid, blockLabel);
  const serialized = serialize(blockLabel, data);

  if (serialized === false) {
    return false;
  }

  localStorage.setItem(key, serialized);

  return true;
};

const storeUse = (wpid: string, blockLabel: string, value: boolean) => {
  const key = getStorageKeyForUse(wpid, blockLabel);
  const serialized = serialize(blockLabel, value);

  if (serialized === false) {
    return false;
  }

  localStorage.setItem(key, serialized);

  return true;
};

// Helper function to load all useOverrides from localStorage
const loadAllUseOverrides = (): {
  [wpid: string]: { [blockLabel: string]: boolean };
} => {
  const useOverrides: {
    [wpid: string]: { [blockLabel: string]: boolean };
  } = {};

  // Iterate through all localStorage keys to find useOverride entries
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.startsWith("skyvern.block-output.use.")) {
      try {
        const value = localStorage.getItem(key);
        if (value) {
          const parsed = JSON.parse(value);
          // Extract wpid and blockLabel from key: skyvern.block-output.use.{wpid}.{blockLabel}
          const keyParts = key.split(".");
          if (keyParts.length >= 5) {
            const wpid = keyParts[3];
            const blockLabel = keyParts.slice(4).join(".");

            if (wpid && blockLabel) {
              useOverrides[wpid] ??= {};
              useOverrides[wpid][blockLabel] = Boolean(parsed);
            }
          }
        }
      } catch (e) {
        console.error(
          `Failed to parse useOverride from localStorage key: ${key}`,
          e,
        );
      }
    }
  }

  return useOverrides;
};

const useBlockOutputStore = create<BlockOutputStore>((set, get) => {
  return {
    outputs: {},
    useOverrides: loadAllUseOverrides(),
    // --
    getOverride: (opts) => {
      const { wpid, blockLabel } = opts;

      if (!wpid) {
        return null;
      }

      const data = load(wpid, blockLabel);

      return data;
    },
    getUseOverride: (opts) => {
      const { wpid, blockLabel } = opts;

      if (!wpid) {
        return false;
      }

      const use = loadUse(wpid, blockLabel);

      return use || false;
    },
    getOutputsWithOverrides: (wpid) => {
      const state = get();
      const baseOutputs = { ...state.outputs };

      if (!wpid) {
        return baseOutputs;
      }

      // Apply overrides for blocks where useOverrides[wpid][blockLabel] is true
      const workflowOverrides = state.useOverrides[wpid];
      if (workflowOverrides) {
        Object.entries(workflowOverrides).forEach(
          ([blockLabel, useOverride]) => {
            if (useOverride) {
              const override = state.getOverride({ wpid, blockLabel });
              if (override) {
                baseOutputs[blockLabel] = override;
              }
            }
          },
        );
      }

      return baseOutputs;
    },
    setOutputs: (outputs) => {
      set(() => ({
        outputs,
      }));
    },
    setOverride: (opts) => {
      const { wpid, blockLabel, data } = opts;

      if (!wpid) {
        return false;
      }

      const wasStored = store(wpid, blockLabel, data);

      return wasStored;
    },
    setUseOverride: (opts) => {
      const { wpid, blockLabel, value } = opts;

      if (!wpid) {
        return false;
      }

      const wasStored = storeUse(wpid, blockLabel, value);

      set((state) => ({
        ...state,
        useOverrides: {
          ...state.useOverrides,
          [wpid]: {
            ...state.useOverrides[wpid],
            [blockLabel]: value,
          },
        },
      }));

      return wasStored;
    },
    reset: () => {
      set({
        outputs: {},
      });
    },
  };
});

export { useBlockOutputStore };
