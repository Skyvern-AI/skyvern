import { useEffect } from "react";

import { useNotFoundStore } from "@/store/NotFoundStore";
import "./Status404.css";

function Status404() {
  const showNotFound = useNotFoundStore((state) => state.showNotFound);
  const hideNotFound = useNotFoundStore((state) => state.hideNotFound);

  // Full-bleed routes (the studio, the editor) hide the app header, and a run
  // URL keeps matching them after the run resolves to a 404. Announce the
  // not-found screen so the shell can bring its chrome back.
  useEffect(() => {
    showNotFound();
    return hideNotFound;
  }, [showNotFound, hideNotFound]);

  return (
    <div
      className="relative z-50 flex w-full items-center justify-center bg-background"
      // hack(jdo): 7.5rem is header height; this is a fail, IMO; we should redo root layout CSS
      style={{ height: "calc(100vh - 7.5rem)" }}
    >
      <div className="absolute flex h-full w-full items-center justify-center">
        <div className="animate-roll-right-404 relative flex h-[13rem] w-[13rem] flex-col items-center justify-center rounded-full bg-white/5 text-xl font-bold text-white">
          <div className="animate-fade-in-404">404</div>
          <div className="opacity-50">Not Found</div>
          <div className="animate-orbit-404 absolute h-full w-full">
            <div className="animate-fade-in-slow-404 relative h-[2rem] w-[2rem] translate-x-[5.5rem] translate-y-[5.5rem] rounded-full bg-white/10" />
          </div>
        </div>
      </div>
      <div className="absolute flex h-full w-full items-center justify-center">
        <img src="/404-sad-dragon-md.png" alt="404 Not Found" />
      </div>
    </div>
  );
}

export { Status404 };
