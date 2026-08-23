import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

vi.mock("../auth/firebase", () => ({
  getIdToken: vi.fn().mockResolvedValue(null),
  auth: {},
}));