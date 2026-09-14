import { Navigate, Route, Routes } from "react-router";

import { LibraryPage } from "../features/library/LibraryPage";
import { SearchPage } from "../features/search/SearchPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { TasksPage } from "../features/tasks/TasksPage";
import { UploadDialog } from "../features/uploads/UploadDialog";
import { UploadProvider } from "../features/uploads/UploadProvider";
import { AppShell } from "./AppShell";

export function App() {
  return (
    <UploadProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/search" replace />} />
          <Route path="search" element={<SearchPage />} />
          <Route path="library" element={<LibraryPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/search" replace />} />
        </Route>
      </Routes>
      <UploadDialog />
    </UploadProvider>
  );
}
