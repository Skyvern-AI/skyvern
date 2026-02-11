interface Window {
  pylon: {
    chat_settings: { [k: string]: string };
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  Pylon: (method: string, ...args: any[]) => void;
}
