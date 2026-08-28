import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, type Mock, vi } from "vitest";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import type {
  QuestionnaireAnswersV1,
  QuestionnairePatchV1,
} from "@/store/onboarding/types";
import { QuestionnaireDetailsStep } from "./QuestionnaireDetailsStep";

const ANSWERS: QuestionnaireAnswersV1 = {
  role: "developer",
  company_context: "personal_or_individual",
  scale_intent: "exploring",
  referral_source: "search",
};
const LABELS = [
  "What best describes your role?",
  "What kind of organization are you part of?",
  "How do you plan to use Skyvern?",
  "How did you hear about Skyvern?",
];

type Action = (patch: QuestionnairePatchV1) => Promise<void>;

function renderDetails({
  action = "complete",
  revision = 0,
  initialAnswers = null,
  externalError = null,
  isPending = false,
  onAction = vi.fn<Action>().mockResolvedValue(),
  onBack = vi.fn(),
}: {
  action?: "complete" | "update";
  revision?: number;
  initialAnswers?: QuestionnaireAnswersV1 | null;
  externalError?: string | null;
  isPending?: boolean;
  onAction?: Mock<Action>;
  onBack?: () => void;
} = {}) {
  render(
    <Dialog open>
      <DialogContent>
        <QuestionnaireDetailsStep
          completionAction={action}
          expectedRevision={revision}
          initialAnswers={initialAnswers}
          externalError={externalError}
          isPending={isPending}
          onAction={onAction}
          onBack={onBack}
        />
      </DialogContent>
    </Dialog>,
  );
  return { onAction, onBack };
}

function selectAll() {
  screen.getAllByRole("combobox").forEach((control) => {
    fireEvent.click(control);
    fireEvent.click(screen.getAllByRole("option")[0]!);
  });
}

function submitted(onAction: Mock<Action>): QuestionnairePatchV1 {
  const patch = onAction.mock.calls[onAction.mock.calls.length - 1]?.[0];
  if (!patch) throw new Error("Expected questionnaire submission");
  return patch;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("QuestionnaireDetailsStep", () => {
  it("renders direction A as one optional labelled form", () => {
    const { onBack } = renderDetails();
    expect(
      screen.getByRole("heading", { name: "Tell us about your setup" }),
    ).toBeTruthy();
    expect(
      screen.getByText(/Optional\. Your answers help us recommend/),
    ).toBeTruthy();
    expect(screen.queryByText(/About 30 seconds/)).toBeNull();
    expect(screen.getAllByRole("combobox")).toHaveLength(4);
    LABELS.forEach((label) =>
      expect(screen.getByLabelText(label)).toBeTruthy(),
    );
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledOnce();
  });

  it("keeps an incomplete Complete action focusable and explains why it cannot submit", () => {
    const { onAction } = renderDetails();
    const submit = screen.getByRole("button", {
      name: "Complete and continue",
    });
    const hint = screen.getByText(
      "Answer every question to complete and continue.",
    );

    expect(submit).toHaveProperty("disabled", false);
    expect(submit.getAttribute("aria-disabled")).toBe("true");
    expect(submit.getAttribute("aria-describedby")).toBe(hint.id);
    expect(submit.classList.contains("cursor-not-allowed")).toBe(true);
    expect(submit.classList.contains("opacity-50")).toBe(true);
    submit.focus();
    expect(document.activeElement).toBe(submit);
    fireEvent.click(submit);
    expect(onAction).not.toHaveBeenCalled();
  });

  it("natively disables Complete while a submission is pending", () => {
    renderDetails({ isPending: true });
    expect(
      screen.getByRole("button", { name: "Complete and continue" }),
    ).toHaveProperty("disabled", true);
  });

  it("announces a parent close error without leaving details", () => {
    renderDetails({
      externalError: "We couldn't save your choice. Try again.",
    });
    expect(screen.getByRole("alert").textContent).toBe(
      "We couldn't save your choice. Try again.",
    );
    expect(
      screen.getByRole("heading", { name: "Tell us about your setup" }),
    ).toBeTruthy();
  });

  it("prefers a new close error over a stale submit error", async () => {
    const onAction = vi
      .fn<(patch: QuestionnairePatchV1) => Promise<void>>()
      .mockRejectedValue(new Error("offline"));
    const view = render(
      <Dialog open>
        <DialogContent>
          <QuestionnaireDetailsStep
            completionAction="complete"
            expectedRevision={0}
            initialAnswers={null}
            externalError={null}
            isPending={false}
            onAction={onAction}
            onBack={vi.fn()}
          />
        </DialogContent>
      </Dialog>,
    );
    selectAll();
    fireEvent.click(
      screen.getByRole("button", { name: "Complete and continue" }),
    );
    expect((await screen.findByRole("alert")).textContent).toBe(
      "We couldn't save your details. Try again.",
    );

    view.rerender(
      <Dialog open>
        <DialogContent>
          <QuestionnaireDetailsStep
            completionAction="complete"
            expectedRevision={0}
            initialAnswers={null}
            externalError="We couldn't save your choice. Try again."
            isPending={false}
            onAction={onAction}
            onBack={vi.fn()}
          />
        </DialogContent>
      </Dialog>,
    );
    expect(screen.getByRole("alert").textContent).toBe(
      "We couldn't save your choice. Try again.",
    );
  });

  it.each([
    ["complete", 0, null],
    ["update", 3, ANSWERS],
  ] as const)(
    "submits %s with all answers and the exact revision",
    async (action, revision, initialAnswers) => {
      const onAction = vi
        .fn<(patch: QuestionnairePatchV1) => Promise<void>>()
        .mockResolvedValue();
      renderDetails({ action, revision, initialAnswers, onAction });
      const submit = screen.getByRole("button", {
        name: "Complete and continue",
      });
      if (!initialAnswers) {
        selectAll();
      }
      fireEvent.click(submit);
      await waitFor(() => expect(onAction).toHaveBeenCalledOnce());
      expect(submitted(onAction)).toEqual({
        version: 1,
        mutation_id: expect.any(String),
        action,
        expected_revision: revision,
        ...ANSWERS,
      });
    },
  );

  it("submits answer-free Skip without a defer control", async () => {
    const onAction = vi
      .fn<(patch: QuestionnairePatchV1) => Promise<void>>()
      .mockResolvedValue();
    renderDetails({ onAction });
    expect(screen.queryByRole("button", { name: "Maybe later" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    await waitFor(() => expect(onAction).toHaveBeenCalledOnce());
    expect(submitted(onAction)).toEqual({
      version: 1,
      mutation_id: expect.any(String),
      expected_revision: 0,
      action: "skip",
    });
  });

  it("keeps selections and mutation identity for an exact retry only", async () => {
    const onAction = vi
      .fn<(patch: QuestionnairePatchV1) => Promise<void>>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(undefined);
    renderDetails({ onAction });
    selectAll();
    const submit = screen.getByRole("button", {
      name: "Complete and continue",
    });
    fireEvent.click(submit);
    expect((await screen.findByRole("alert")).textContent).toBe(
      "We couldn't save your details. Try again.",
    );
    const firstMutation = submitted(onAction).mutation_id;

    fireEvent.click(submit);
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(2));
    expect(submitted(onAction).mutation_id).toBe(firstMutation);

    fireEvent.click(screen.getAllByRole("combobox")[0]!);
    fireEvent.click(screen.getAllByRole("option")[1]!);
    fireEvent.click(submit);
    await waitFor(() => expect(onAction).toHaveBeenCalledTimes(3));
    expect(submitted(onAction).mutation_id).not.toBe(firstMutation);
  });
});
