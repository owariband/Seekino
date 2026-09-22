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
import { AppShell } from "./AppShell";

export function App() {
  return (
    <UploadProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/watch" replace />} />
          <Route path="watch" element={<WatchHomePage />} />
          <Route path="search" element={<SearchPage />} />
          <Route path="library" element={<LibraryPage />} />
          <Route path="watch/:seriesId/:videoId" element={<WatchPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="usage" element={<UsagePage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/watch" replace />} />
        </Route>
      </Routes>
      <UploadDialog />
    </UploadProvider>
  );
}
