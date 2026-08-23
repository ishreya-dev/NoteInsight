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

describe("api.client streamAnalysis", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function createReader(chunks: string[]) {
    const reads: Promise<{ value: Uint8Array | undefined; done: boolean }>[] = chunks.map(
      (chunk) => {
        const encoded = new TextEncoder().encode(chunk);
        return Promise.resolve({ value: encoded, done: false });
      },
    );
    reads.push(Promise.resolve({ value: undefined, done: true }));

    return {
      read: vi.fn().mockImplementation(() => reads.shift()!),
      releaseLock: vi.fn(),
    };
  }

  function mockStreamResponse(chunks: string[]) {
    const reader = createReader(chunks);
    const response = {
      ok: true,
      status: 200,
      body: {
        getReader: () => reader,
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
    return reader;
  }

  const analysisPayload = {
    note_id: "n1",
    analysis: {
      id: "a1",
      note_id: "n1",
      user_id: "u1",
      conditions: [],
      gaps: [],
      summary: "Summary",
      model_version: "test",
      prompt_version: "v1",
      created_at: "2026-01-01T00:00:00Z",
      is_failed: false,
      failure_reason: null,
    },
  };

  it("token SSE event calls onToken with the token text", async () => {
    mockStreamResponse([
      "event: token\ndata: {\"text\": \"Hello\"}\n\n",
    ]);

    const onToken = vi.fn();
    await api.streamAnalysis("n1", {
      onStatus: vi.fn(),
      onToken,
      onComplete: vi.fn(),
      onError: vi.fn(),
    });

    expect(onToken).toHaveBeenCalledWith("Hello");
  });

  it("status SSE event calls onStatus with the status payload", async () => {
    mockStreamResponse([
      "event: status\ndata: {\"stage\": \"preparing\", \"message\": \"Starting\"}\n\n",
    ]);

    const onStatus = vi.fn();
    await api.streamAnalysis("n1", {
      onStatus,
      onToken: vi.fn(),
      onComplete: vi.fn(),
      onError: vi.fn(),
    });

    expect(onStatus).toHaveBeenCalledWith({ stage: "preparing", message: "Starting" });
  });

  it("complete SSE event calls onComplete with the parsed analysis", async () => {
    mockStreamResponse([
      `event: complete\ndata: ${JSON.stringify(analysisPayload)}\n\n`,
    ]);

    const onComplete = vi.fn();
    await api.streamAnalysis("n1", {
      onStatus: vi.fn(),
      onToken: vi.fn(),
      onComplete,
      onError: vi.fn(),
    });

    expect(onComplete).toHaveBeenCalledWith(analysisPayload);
  });

  it("error SSE event calls onError with the error payload", async () => {
    mockStreamResponse([
      "event: error\ndata: {\"reason\": \"timeout\", \"message\": \"Timed out\"}\n\n",
    ]);

    const onError = vi.fn();
    await api.streamAnalysis("n1", {
      onStatus: vi.fn(),
      onToken: vi.fn(),
      onComplete: vi.fn(),
      onError,
    });

    expect(onError).toHaveBeenCalledWith({ reason: "timeout", message: "Timed out" });
  });

  it("handles multiple SSE events in one response chunk", async () => {
    const chunk =
      "event: status\ndata: {\"stage\": \"preparing\"}\n\nevent: token\ndata: {\"text\": \"A\"}\n\nevent: token\ndata: {\"text\": \"B\"}\n\nevent: complete\ndata: " +
      JSON.stringify(analysisPayload) +
      "\n\n";
    mockStreamResponse([chunk]);

    const onStatus = vi.fn();
    const onToken = vi.fn();
    const onComplete = vi.fn();
    await api.streamAnalysis("n1", {
      onStatus,
      onToken,
      onComplete,
      onError: vi.fn(),
    });

    expect(onStatus).toHaveBeenCalledWith({ stage: "preparing" });
    expect(onToken).toHaveBeenCalledTimes(2);
    expect(onToken).toHaveBeenNthCalledWith(1, "A");
    expect(onToken).toHaveBeenNthCalledWith(2, "B");
    expect(onComplete).toHaveBeenCalledWith(analysisPayload);
  });

  it("buffers an SSE event split across multiple network chunks", async () => {
    mockStreamResponse([
      "event: token\ndata: {\"text\": \"Hel",
      "lo\"}\n\n",
    ]);

    const onToken = vi.fn();
    await api.streamAnalysis("n1", {
      onStatus: vi.fn(),
      onToken,
      onComplete: vi.fn(),
      onError: vi.fn(),
    });

    expect(onToken).toHaveBeenCalledWith("Hello");
  });

  it("rejects on network fetch failure without calling SSE handlers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );

    const onComplete = vi.fn();
    const onError = vi.fn();

    await expect(
      api.streamAnalysis("n1", {
        onStatus: vi.fn(),
        onToken: vi.fn(),
        onComplete,
        onError,
      }),
    ).rejects.toThrow();

    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("rejects on malformed SSE JSON without crashing the caller", async () => {
    mockStreamResponse([
      "event: token\ndata: {invalid json}\n\n",
    ]);

    const onComplete = vi.fn();
    const onError = vi.fn();

    await expect(
      api.streamAnalysis("n1", {
        onStatus: vi.fn(),
        onToken: vi.fn(),
        onComplete,
        onError,
      }),
    ).rejects.toThrow();

    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("AbortSignal cancellation aborts the request without calling onComplete", async () => {
    const controller = new AbortController();
    controller.abort();

    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(
        new DOMException("The user aborted a request.", "AbortError"),
      ),
    );

    const onComplete = vi.fn();
    const onError = vi.fn();

    await expect(
      api.streamAnalysis("n1", {
        signal: controller.signal,
        onStatus: vi.fn(),
        onToken: vi.fn(),
        onComplete,
        onError,
      }),
    ).rejects.toThrow();

    expect(onComplete).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("error SSE event followed by stream termination does not call onComplete", async () => {
    mockStreamResponse([
      "event: error\ndata: {\"reason\": \"timeout\", \"message\": \"Timed out\"}\n\n",
    ]);

    const onComplete = vi.fn();
    const onError = vi.fn();

    await api.streamAnalysis("n1", {
      onStatus: vi.fn(),
      onToken: vi.fn(),
      onComplete,
      onError,
    });

    expect(onError).toHaveBeenCalledWith({ reason: "timeout", message: "Timed out" });
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("empty partial trailing SSE data does not create a false event", async () => {
    mockStreamResponse([
      "event: token\ndata: {\"text\": \"Hel",
    ]);

    const onToken = vi.fn();
    const onComplete = vi.fn();

    await api.streamAnalysis("n1", {
      onStatus: vi.fn(),
      onToken,
      onComplete,
      onError: vi.fn(),
    });

    expect(onToken).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();
  });
});