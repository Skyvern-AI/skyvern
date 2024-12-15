import { apiBaseUrl, envCredential } from "./util/env";

interface Window {
  devCommands: {
    createBrowserSession: () => Promise<void>;
    listBrowserSessions: () => Promise<void>;
  };
}

export function initDevCommands() {
  if (!envCredential) {
    console.warn("envCredential environment variable was not set");
    return;
  }

  async function createBrowserSession() {
    try {
      const response = await fetch(`${apiBaseUrl}/browser_sessions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": envCredential!,
        },
        credentials: "include",
      });

      if (!response.ok) {
        throw new Error(
          `Failed to create browser session: ${response.statusText}`,
        );
      }

      const data = await response.json();
      console.log("Created browser session:", data);
      return undefined;
    } catch (error) {
      console.error("Error creating browser session:", error);
      throw error;
    }
  }

  async function listBrowserSessions() {
    try {
      const response = await fetch(`${apiBaseUrl}/browser_sessions`, {
        method: "GET",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": envCredential!,
        },
        credentials: "include",
      });

      if (!response.ok) {
        throw new Error(
          `Failed to list browser sessions: ${response.statusText}`,
        );
      }

      const data = await response.json();
      console.log("Browser sessions:", data);
      return undefined;
    } catch (error) {
      console.error("Error listing browser sessions:", error);
      throw error;
    }
  }

  (window as unknown as Window).devCommands = {
    createBrowserSession,
    listBrowserSessions,
  };

  console.log("Dev commands initialized. Available commands:");
  console.log("- window.devCommands.createBrowserSession()");
  console.log("- window.devCommands.listBrowserSessions()");
}
