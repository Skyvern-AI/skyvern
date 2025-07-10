import { RouterProvider } from "react-router-dom";
import { ThemeProvider } from "@/components/ThemeProvider";
import { router } from "./router";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "./api/QueryClient";

import { PostHogProvider } from "posthog-js/react";
import { UserContext } from "@/store/UserContext";

const postHogOptions = {
  api_host: "https://app.posthog.com",
};

const getUser = () => {
  return null;
};

function App() {
  return (
    <UserContext.Provider value={getUser}>
      <PostHogProvider
        apiKey="phc_bVT2ugnZhMHRWqMvSRHPdeTjaPxQqT3QSsI3r5FlQR5"
        options={postHogOptions}
      >
        <QueryClientProvider client={queryClient}>
          <ThemeProvider defaultTheme="dark">
            <RouterProvider router={router} />
          </ThemeProvider>
        </QueryClientProvider>
      </PostHogProvider>
    </UserContext.Provider>
  );
}

export default App;
