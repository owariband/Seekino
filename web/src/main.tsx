import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";

import { App } from "./app/App";
import { ThemeProvider } from "./app/theme";
import { WorkspaceProvider } from "./app/workspace";
import "./styles/global.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <WorkspaceProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </WorkspaceProvider>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
);
