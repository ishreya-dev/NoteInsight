import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import NoteForm from "./NoteForm";

function words(n: number): string {
  return Array.from({ length: n }, () => "a").join(" ");
}

describe("NoteForm", () => {
  it("disables submission when the note is empty", () => {
    render(
      <NoteForm
        onSubmit={vi.fn()}
        submitting={false}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: /analyze note/i,
      }),
    ).toBeDisabled();
  });

  it("submits trimmed note data", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();

    render(
      <NoteForm
        onSubmit={onSubmit}
        submitting={false}
      />,
    );

    await user.type(
      screen.getByLabelText(/clinical note/i),
      "   Patient has a headache.   ",
    );

    await user.type(
      screen.getByLabelText(/patient pseudonym/i),
      "   Patient A   ",
    );

    await user.click(
      screen.getByRole("button", {
        name: /analyze note/i,
      }),
    );

    expect(onSubmit).toHaveBeenCalledTimes(1);

    expect(onSubmit).toHaveBeenCalledWith({
      raw_text: "Patient has a headache.",
      pseudonym: "Patient A",
      visit_date: null,
    });
  });

  it("shows a validation error for whitespace-only text", async () => {
    const user = userEvent.setup();

    render(
      <NoteForm
        onSubmit={vi.fn()}
        submitting={false}
      />,
    );

    await user.type(
      screen.getByLabelText(/clinical note/i),
      "     ",
    );

    expect(
      screen.getByRole("button", {
        name: /analyze note/i,
      }),
    ).toBeDisabled();
  });

  it("rejects notes longer than 20,000 characters", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();

    render(
      <NoteForm
        onSubmit={onSubmit}
        submitting={false}
      />,
    );

    const textarea = screen.getByLabelText(/clinical note/i);
    fireEvent.change(textarea, { target: { value: "a".repeat(20_001) } });

    await user.click(
      screen.getByRole("button", {
        name: /analyze note/i,
      }),
    );

    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getByRole("alert"),
    ).toHaveTextContent(/max 20,000/);
  });

  it("accepts a note with exactly 6,000 words", async () => {
    const onSubmit = vi.fn();

    render(
      <NoteForm
        onSubmit={onSubmit}
        submitting={false}
      />,
    );

    const textarea = screen.getByLabelText(/clinical note/i);
    fireEvent.change(textarea, { target: { value: words(6_000) } });

    fireEvent.click(
      screen.getByRole("button", {
        name: /analyze note/i,
      }),
    );

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("rejects a note with 6,001 words", async () => {
    const onSubmit = vi.fn();

    render(
      <NoteForm
        onSubmit={onSubmit}
        submitting={false}
      />,
    );

    const textarea = screen.getByLabelText(/clinical note/i);
    fireEvent.change(textarea, { target: { value: words(6_001) } });

    fireEvent.click(
      screen.getByRole("button", {
        name: /analyze note/i,
      }),
    );

    expect(onSubmit).not.toHaveBeenCalled();
    expect(
      screen.getByRole("alert"),
    ).toHaveTextContent(/max 6000/);
  });

  it("disables fields while submitting", () => {
    render(
      <NoteForm
        onSubmit={vi.fn()}
        submitting={true}
      />,
    );

    expect(
      screen.getByLabelText(/clinical note/i),
    ).toBeDisabled();

    expect(
      screen.getByLabelText(/patient pseudonym/i),
    ).toBeDisabled();

    expect(
      screen.getByLabelText(/visit date/i),
    ).toBeDisabled();

    expect(
      screen.getByRole("button", {
        name: /analyzing/i,
      }),
    ).toBeDisabled();
  });
});