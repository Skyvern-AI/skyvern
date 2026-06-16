import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.tsx";
import "./index.css";
import { installChunkLoadErrorHandler } from "./util/lazyWithReload";
import { installTranslationCrashGuard } from "./util/translationCrashGuard";

installTranslationCrashGuard();
installChunkLoadErrorHandler();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
