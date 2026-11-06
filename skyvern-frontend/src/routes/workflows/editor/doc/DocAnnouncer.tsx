import { useEffect, useSyncExternalStore } from "react";

import {
  clearDocAnnouncement,
  docAnnouncement,
  subscribeDocAnnouncement,
} from "./docAnnounce";

export function DocAnnouncer() {
  useEffect(() => clearDocAnnouncement, []);
  const text = useSyncExternalStore(
    subscribeDocAnnouncement,
    docAnnouncement,
    () => "",
  );
  return (
    <div
      data-doc-announcer
      role="status"
      aria-live="polite"
      className="sr-only"
    >
      {text}
    </div>
  );
}
