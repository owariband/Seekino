import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, useLayoutEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, useLocation, useNavigate } from "react-router";

import { App } from "./app/App";
import { ThemeProvider } from "./app/theme";
import { WorkspaceProvider } from "./app/workspace";
import { AuthProvider, useAuth } from "./features/auth/AuthProvider";
import "./styles/global.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
    },
  },
});

function IdentityApp() {
  const { scopeKey, activeSource } = useAuth();
  const ownerScope = `${activeSource.key}:${activeSource.identity.userId}:${activeSource.tenantId}`;
  const previousScope = useRef(ownerScope);
  const location = useLocation();
  const navigate = useNavigate();
  useLayoutEffect(() => {
    if (previousScope.current !== ownerScope && location.pathname.startsWith("/watch/")) navigate("/watch", { replace: true });
    previousScope.current = ownerScope;
  }, [ownerScope, location.pathname, navigate]);
  return <WorkspaceProvider key={scopeKey}>
    <App />
  </WorkspaceProvider>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <BrowserRouter><AuthProvider><IdentityApp /></AuthProvider></BrowserRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
);
