import { useEffect, useState } from "react";
import { RouterProvider } from "react-router-dom";
import { ThemeProvider } from "@/components/ThemeProvider";
import { router } from "./router";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./api/QueryClient";

import posthog from "posthog-js";
import { PostHogProvider } from "posthog-js/react";
import { LoggingContext, loggingStub } from "@/store/LoggingContext";
import { UserContext } from "@/store/UserContext";
import { RuntimeFeatureFlagProvider } from "@/providers/RuntimeFeatureFlagProvider";
import { OnPremOidcProvider } from "@/providers/OnPremOidcProvider";
import { PostHogInitializedContext } from "@/hooks/usePostHogFeatureFlag";
import { initializePostHogFromRuntimeConfig } from "@/util/posthogInitialization";

const getLogging = () => {
  return loggingStub;
};

const getUser = () => {
  return null;
};

function App() {
  const [postHogInitialized, setPostHogInitialized] = useState(false);

  useEffect(() => {
    void initializePostHogFromRuntimeConfig().then(setPostHogInitialized);
  }, []);

  return (
    <LoggingContext.Provider value={getLogging}>
      <UserContext.Provider value={getUser}>
        <PostHogProvider client={posthog}>
          <PostHogInitializedContext.Provider value={postHogInitialized}>
            <QueryClientProvider client={queryClient}>
              <RuntimeFeatureFlagProvider>
                <ThemeProvider defaultTheme="dark">
                  <OnPremOidcProvider>
                    <RouterProvider router={router} />
                  </OnPremOidcProvider>
                </ThemeProvider>
              </RuntimeFeatureFlagProvider>
            </QueryClientProvider>
          </PostHogInitializedContext.Provider>
        </PostHogProvider>
      </UserContext.Provider>
    </LoggingContext.Provider>
  );
}

export default App;
