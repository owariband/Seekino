import { lazy } from "react";
import { Navigate, Route, Routes } from "react-router";

import { WatchHomePage } from "../features/player/WatchHomePage";
import { UploadDialog } from "../features/uploads/UploadDialog";
import { UploadProvider } from "../features/uploads/UploadProvider";
import { useAuth } from "../features/auth/AuthProvider";
import { AppShell } from "./AppShell";

const LibraryPage = lazy(() => import("../features/library/LibraryPage").then((module) => ({ default: module.LibraryPage })));
const WatchPage = lazy(() => import("../features/player/WatchPage").then((module) => ({ default: module.WatchPage })));
const SeriesPage = lazy(() => import("../features/player/SeriesPage").then((module) => ({ default: module.SeriesPage })));
const RoutedSearchPage = lazy(() => import("../features/search/SearchPage").then((module) => ({ default: module.RoutedSearchPage })));
const SettingsPage = lazy(() => import("../features/settings/SettingsPage").then((module) => ({ default: module.SettingsPage })));
const TasksPage = lazy(() => import("../features/tasks/TasksPage").then((module) => ({ default: module.TasksPage })));
const UsagePage = lazy(() => import("../features/usage/UsagePage").then((module) => ({ default: module.UsagePage })));

export function App() {
  const { canManage, canUpload } = useAuth();
  return (
    <UploadProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/watch" replace />} />
          <Route path="watch" element={<WatchHomePage />} />
          <Route path="series/:seriesId" element={<SeriesPage />} />
          <Route path="search" element={<RoutedSearchPage />} />
          <Route path="library" element={canManage ? <LibraryPage /> : <Navigate to="/watch" replace />} />
          <Route path="watch/:seriesId/:videoId" element={<WatchPage />} />
          <Route path="tasks" element={canManage ? <TasksPage /> : <Navigate to="/watch" replace />} />
          <Route path="usage" element={canManage ? <UsagePage /> : <Navigate to="/watch" replace />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/watch" replace />} />
        </Route>
      </Routes>
      {canUpload ? <UploadDialog /> : null}
    </UploadProvider>
  );
}
