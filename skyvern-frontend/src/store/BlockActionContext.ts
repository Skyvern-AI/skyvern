import { createContext } from "react";

type RequestDeleteNodeCallback = (id: string, label: string) => void;
type DuplicateNodeCallback = (id: string) => void;
type TransmuteNodeCallback = (id: string, nodeName: string) => void;

const BlockActionContext = createContext<
  | {
      requestDeleteNodeCallback: RequestDeleteNodeCallback;
      duplicateNodeCallback: DuplicateNodeCallback;
      transmuteNodeCallback: TransmuteNodeCallback;
    }
  | undefined
>(undefined);

export { BlockActionContext };
