import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./errors";

vi.mock("../auth/firebase", () => ({
  getIdToken: vi.fn().mockResolvedValue("test-token"),
}));

const { api } = await import("./client");

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("throws an ApiError with the server's detail message on a 4xx response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Note text is required." }), {
          status: 422,
        }),
      ),
    );

    await expect(api.listNotes()).rejects.toMatchObject(
      new ApiError(422, "Note text is required."),
    );
  });

  it("falls back to statusText when the error response has no JSON body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 500, statusText: "Server Error" })),
    );

    await expect(api.listNotes()).rejects.toMatchObject(
      new ApiError(500, "Server Error"),
    );
  });

  it("throws a status-0 ApiError when the network request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );

    let error: unknown;
    try {
      await api.listNotes();
    } catch (e) {
      error = e;
    }

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
  });

  it("returns parsed JSON on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200 })),
    );

    await expect(api.listNotes()).resolves.toEqual({ items: [] });
  });
});