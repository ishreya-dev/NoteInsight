import { useEffect } from "react";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "./auth/AuthProvider";
import AppRoutes from "./routes";

export default function App() {
  useEffect(() => {
    const BASE_URL = import.meta.env.VITE_API_BASE_URL as string;

    const ping = async () => {
      try {
        await fetch(`${BASE_URL}/health`, { method: "GET" });
      } catch {
        // ignore
      }
    };

    ping();

    const interval = setInterval(ping, 14 * 60 * 1000);

    return () => clearInterval(interval);
  }, []);

  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}