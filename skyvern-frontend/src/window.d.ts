import { DevCommands } from "./initDevCommands";

export {};

declare global {
  interface Window {
    devCommands: DevCommands;
  }
}
