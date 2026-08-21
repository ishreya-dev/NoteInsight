import { Navigate, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";

import { useAuth } from "./auth/useAuth";
import LoginPage from "./pages/LoginPage";
import NewNotePage from "./pages/NewNotePage";
import HistoryPage from "./pages/HistoryPage";
import NoteDetailPage from "./pages/NoteDetailPage";

function ProtectedRoute({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div role="status" aria-live="polite">
        Loading...
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route
        path="/notes/new"
        element={
          <ProtectedRoute>
            <NewNotePage />
          </ProtectedRoute>
        }
      />

      <Route
        path="/notes/:noteId"
        element={
          <ProtectedRoute>
            <NoteDetailPage />
          </ProtectedRoute>
        }
      />

      <Route
        path="/history"
        element={
          <ProtectedRoute>
            <HistoryPage />
          </ProtectedRoute>
        }
      />

      <Route
        path="/"
        element={<Navigate to="/notes/new" replace />}
      />

      <Route
        path="*"
        element={<Navigate to="/notes/new" replace />}
      />
    </Routes>
  );
}