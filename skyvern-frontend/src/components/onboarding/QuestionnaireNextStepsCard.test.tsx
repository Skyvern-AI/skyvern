// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import type { QuestionnaireAnswersV1 } from "@/store/onboarding/types";
import { QuestionnaireNextStepsCard } from "./QuestionnaireNextStepsCard";

const baseAnswers: QuestionnaireAnswersV1 = {
  role: "developer",
  company_context: "startup",
  scale_intent: "exploring",
  referral_source: "search",
};

function renderCard(answers: QuestionnaireAnswersV1 | null) {
  const onAction = vi.fn();
  const onBack = vi.fn();
  const onSkip = vi.fn();
  const view = render(
    <MemoryRouter>
      <Dialog open>
        <DialogContent>
          <QuestionnaireNextStepsCard
            answers={answers}
            onAction={onAction}
            onBack={onBack}
            onSkip={onSkip}
          />
        </DialogContent>
      </Dialog>
    </MemoryRouter>,
  );
  return { ...view, onAction, onBack, onSkip };
}

afterEach(cleanup);

describe("QuestionnaireNextStepsCard", () => {
  it("routes a technical explorer through integrations and a first run", () => {
    const { onAction } = renderCard(baseAnswers);

    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(3);
    for (const item of items) {
      const links = within(item).getAllByRole("link");
      expect(links).toHaveLength(2);
      expect(links[1]?.getAttribute("target")).toBe("_blank");
      expect(links[1]?.getAttribute("rel")).toBe("noopener noreferrer");
    }
    expect(onAction).not.toHaveBeenCalled();
    expect(screen.getByText("Start with a template")).toBeTruthy();
    expect(screen.getByText("Connect Skyvern to your stack")).toBeTruthy();
    expect(screen.getByText("Run and review your agent")).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: "Browse templates" })
        .getAttribute("href"),
    ).toBe("/discover");
    expect(
      screen.getByRole("link", { name: "Set up MCP" }).getAttribute("href"),
    ).toBe("https://www.skyvern.com/docs/cloud/getting-started/mcp");
    expect(
      screen
        .getByRole("link", { name: "How to run an agent" })
        .getAttribute("href"),
    ).toBe("https://www.skyvern.com/docs/cloud/building-agents/run-an-agent");

    fireEvent.click(screen.getByRole("link", { name: "Open integrations" }));
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it.each([
    "recurring_individual",
    "team_or_multi_workflow",
    "production_high_volume",
  ] as const)(
    "routes a business %s answer through credentials and scheduling",
    (scaleIntent) => {
      renderCard({
        ...baseAnswers,
        role: "business_operator",
        company_context: "established_company",
        scale_intent: scaleIntent,
        referral_source: "friend_or_colleague",
      });

      expect(screen.getAllByRole("listitem")).toHaveLength(3);
      expect(screen.getByText("Build a reusable agent")).toBeTruthy();
      expect(screen.getByText("Add website credentials")).toBeTruthy();
      expect(screen.getByText("Schedule repeat runs")).toBeTruthy();
      expect(
        screen
          .getByRole("link", { name: "How credentials work" })
          .getAttribute("href"),
      ).toBe(
        "https://www.skyvern.com/docs/cloud/managing-credentials/credentials-overview",
      );
      expect(
        screen
          .getByRole("link", { name: "How to schedule an agent" })
          .getAttribute("href"),
      ).toBe("https://www.skyvern.com/docs/cloud/building-agents/scheduling");
    },
  );

  it("hides incomplete answers and defaults unknown persisted values safely", () => {
    const callbacks = {
      onAction: vi.fn(),
      onBack: vi.fn(),
      onSkip: vi.fn(),
    };
    const { container, rerender, unmount } = render(
      <QuestionnaireNextStepsCard answers={null} {...callbacks} />,
    );
    expect(container.textContent).toBe("");

    rerender(
      <QuestionnaireNextStepsCard
        answers={{ role: "developer" } as QuestionnaireAnswersV1}
        {...callbacks}
      />,
    );
    expect(container.textContent).toBe("");
    unmount();

    renderCard({
      ...baseAnswers,
      role: "future_role",
      scale_intent: "future_scale",
      future_answer: "ignored",
    } as unknown as QuestionnaireAnswersV1);
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByText("Add website credentials")).toBeTruthy();
    expect(screen.getByText("Run and review your agent")).toBeTruthy();
  });
});
