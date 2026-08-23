import { getIdToken } from "../auth/firebase";
import { ApiError } from "./errors";
import type {
  NoteCreateResponse,
  NoteDetailResponse,
  NoteHistoryResponse,
  AnalysisDetailResponse,
  ReviewCreate,
  Review,
  NoteCreatePayload,
  AnalysisFailureReason,
} from "./types";

const BASE_URL = import.meta.env.VITE_API_BASE_URL as string;

const DEFAULT_TIMEOUT = 15_000;

export type AnalysisStreamHandlers = {
  signal?: AbortSignal;
  onStatus: (status: import("./types").AnalysisStreamStatus) => void;
  onToken: (text: string) => void;
  onComplete: (result: import("./types").AnalysisStreamComplete) => void;
  onError: (error: {
    reason: AnalysisFailureReason;
    message: string;
  }) => void;
};

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const controller = new AbortController();

  const timeoutId = window.setTimeout(() => {
    controller.abort();
  }, DEFAULT_TIMEOUT);

  try {
    const token = await getIdToken();

    const response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init.headers,
      },
    });

    if (!response.ok) {
      let detail =
        response.statusText ||
        `Request failed with status ${response.status}`;

      try {
        const body: unknown = await response.json();

        if (
          typeof body === "object" &&
          body !== null &&
          "detail" in body &&
          typeof body.detail === "string"
        ) {
          detail = body.detail;
        }
      } catch {
        // The response does not contain a JSON error body.
      }

      throw new ApiError(response.status, detail);
    }

    const text = await response.text();

    return text ? (JSON.parse(text) as T) : (undefined as T);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(
        408,
        "The request took too long. Please try again.",
      );
    }

    if (error instanceof ApiError) {
      throw error;
    }

    throw new ApiError(
      0,
      "Could not reach the server. Check your connection.",
    );
  } finally {
    window.clearTimeout(timeoutId);
  }
}

export const api = {
  createNote: (payload: NoteCreatePayload) =>
    request<NoteCreateResponse>("/notes", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listNotes: (limit = 50) =>
    request<NoteHistoryResponse>(`/notes?limit=${limit}`),

  getNote: (noteId: string) =>
    request<NoteDetailResponse>(`/notes/${noteId}`),

  reanalyzeNote: (noteId: string) =>
    request<NoteCreateResponse>(`/notes/${noteId}/analyze`, {
      method: "POST",
    }),

  streamAnalysis: async (
    noteId: string,
    handlers: AnalysisStreamHandlers,
  ) => {
    const token = await getIdToken();
    const response = await fetch(`${BASE_URL}/notes/${noteId}/analysis/stream`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: handlers.signal,
    });
    if (!response.ok || !response.body) {
      throw new ApiError(response.status, "Could not start analysis.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
        const messages = buffer.split("\n\n");
        buffer = messages.pop() ?? "";
        for (const message of messages) {
          const event = message.match(/^event: (.+)$/m)?.[1];
          const data = message.match(/^data: (.+)$/m)?.[1];
          if (!event || !data) continue;
          const parsed: unknown = JSON.parse(data);
          if (event === "status") {
            handlers.onStatus(parsed as import("./types").AnalysisStreamStatus);
          } else if (event === "token") {
            handlers.onToken?.(
              (parsed as { text: string }).text
            );
          } else if (event === "complete") {
            handlers.onComplete(parsed as import("./types").AnalysisStreamComplete);
          } else if (event === "error") {
            handlers.onError(
              typeof parsed === "object" && parsed !== null
                ? {
                    reason:
                      "reason" in parsed &&
                      [
                        "rate_limited",
                        "invalid_output",
                        "timeout",
                        "provider_error",
                        "unknown",
                      ].includes(String(parsed.reason))
                        ? (String(parsed.reason) as AnalysisFailureReason)
                        : "unknown",
                    message:
                      "message" in parsed
                        ? String(parsed.message)
                        : "Analysis failed. Please try again.",
                  }
                : {
                    reason: "unknown",
                    message: "Analysis failed. Please try again.",
                  },
            );
          }
        }
        if (done) break;
      }
    } finally {
      reader.releaseLock();
    }
  },

  getAnalysis: (analysisId: string) =>
    request<AnalysisDetailResponse>(`/analyses/${analysisId}`),

  upsertReview: (analysisId: string, payload: ReviewCreate) =>
    request<Review>(`/analyses/${analysisId}/reviews`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};