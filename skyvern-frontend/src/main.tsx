import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.tsx";
import { initializeUiSession } from "./api/AxiosClient";
import "./index.css";
import { installChunkLoadErrorHandler } from "./util/lazyWithReload";
import { installTranslationCrashGuard } from "./util/translationCrashGuard";

installTranslationCrashGuard();
installChunkLoadErrorHandler();
void initializeUiSession().catch((err) =>
  console.error("[ui-session] failed to initialize:", err),
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
