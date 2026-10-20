import { useEffect } from "react";
import { driver, type Side } from "driver.js";

import { BASE_DRIVER_CONFIG } from "@/hooks/useEditorOnboardingTour";
import "@/util/onboarding/product-tour.css";

// Public demo sites used by home-page examples; their logins are published on the sites themselves.
const DEMO_LOGINS: Record<string, { username: string; password: string }> = {
  "https://opensource-demo.orangehrmlive.com": {
    username: "Admin",
    password: "admin123",
  },
};

const POLL_INTERVAL_MS = 300;

function getDemoLogin(url: string | undefined) {
  if (!url) return null;
  try {
    return DEMO_LOGINS[new URL(url).origin] ?? null;
  } catch {
    return null;
  }
}

function inputHas(selector: string, value: string) {
  return document.querySelector<HTMLInputElement>(selector)?.value === value;
}

// Walks the user from the copilot credential card through the credential modal for a known demo site.
// Each step advances when the user does it, so the popovers carry no Next button.
function useDemoLoginGuide(loginUrl: string | undefined, active: boolean) {
  const login = active ? getDemoLogin(loginUrl) : null;
  const username = login?.username;
  const password = login?.password;

  useEffect(() => {
    if (!username || !password) return;
    const steps: {
      element: string;
      title: string;
      description: string;
      side: Side;
      done: () => boolean;
    }[] = [
      {
        element: "[data-tour='credential-connect']",
        title: "Add the demo login",
        description:
          "This is a public demo site, so its login is shared. Click Connect credential to save it.",
        side: "right",
        done: () =>
          document.querySelector("[data-tour='credential-username']") !== null,
      },
      {
        element: "[data-tour='credential-username']",
        title: "Enter the username",
        description: `Type ${username}`,
        side: "right",
        done: () => inputHas("[data-tour='credential-username']", username),
      },
      {
        element: "[data-tour='credential-password']",
        title: "Enter the password",
        description: `Type ${password}`,
        side: "right",
        done: () => inputHas("[data-tour='credential-password']", password),
      },
      {
        element: "[data-tour='credential-save']",
        title: "Save it",
        description: "Copilot signs in with it and keeps building.",
        side: "right",
        done: () => false,
      },
    ];

    let current = -1;
    let stopped = false;
    const guide = driver({
      ...BASE_DRIVER_CONFIG,
      allowKeyboardControl: false,
      // A stray click on the dimmed page shouldn't end the guide; the close button does.
      overlayClickBehavior: () => {},
      onCloseClick: () => {
        stopped = true;
        guide.destroy();
      },
    });

    // ponytail: DOM polling so one guide can span the card and the modal it opens; use events if it shows in profiles.
    const interval = setInterval(() => {
      if (stopped) return;
      const next = steps.findIndex((step) => !step.done());
      const element =
        next >= 0 ? document.querySelector(steps[next]!.element) : null;
      if (!element || next === current) return;
      current = next;
      const step = steps[next]!;
      guide.highlight({
        element,
        popover: {
          title: step.title,
          description: step.description,
          side: step.side,
          align: "center",
          showButtons: ["close"],
          showProgress: true,
          progressText: `Step ${next + 1} of ${steps.length}`,
        },
      });
    }, POLL_INTERVAL_MS);

    return () => {
      clearInterval(interval);
      guide.destroy();
    };
  }, [username, password]);
}

export { useDemoLoginGuide };
