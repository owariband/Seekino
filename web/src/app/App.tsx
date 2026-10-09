import { Navigate, Route, Routes } from "react-router";

import { LibraryPage } from "../features/library/LibraryPage";
import { WatchHomePage } from "../features/player/WatchHomePage";
import { WatchPage } from "../features/player/WatchPage";
import { SearchPage } from "../features/search/SearchPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { TasksPage } from "../features/tasks/TasksPage";
import { UsagePage } from "../features/usage/UsagePage";
import { UploadDialog } from "../features/uploads/UploadDialog";
import { UploadProvider } from "../features/uploads/UploadProvider";
import { useAuth } from "../features/auth/AuthProvider";
import { AppShell } from "./AppShell";

export function App() {
  const { canManage, canConfigure, canUpload } = useAuth();
  return (
    <UploadProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/watch" replace />} />
          <Route path="watch" element={<WatchHomePage />} />
          <Route path="search" element={<SearchPage />} />
          <Route path="library" element={canManage ? <LibraryPage /> : <Navigate to="/watch" replace />} />
          <Route path="watch/:seriesId/:videoId" element={<WatchPage />} />
          <Route path="tasks" element={canManage ? <TasksPage /> : <Navigate to="/watch" replace />} />
          <Route path="usage" element={canManage ? <UsagePage /> : <Navigate to="/watch" replace />} />
          <Route path="settings" element={canConfigure ? <SettingsPage /> : <Navigate to="/watch" replace />} />
          <Route path="*" element={<Navigate to="/watch" replace />} />
        </Route>
      </Routes>
      {canUpload ? <UploadDialog /> : null}
    </UploadProvider>
  );
}
