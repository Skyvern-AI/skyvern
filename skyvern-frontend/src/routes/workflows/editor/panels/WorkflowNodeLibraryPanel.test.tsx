// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";

const catalogState = vi.hoisted(() => ({
  data: undefined as unknown[] | undefined,
}));

vi.mock("@/hooks/useIntegrationCatalogQuery", () => ({
  useIntegrationCatalogQuery: () => ({ data: catalogState.data }),
}));
vi.mock("@/hooks/useFeatureFlag", () => ({ useFeatureFlag: () => true }));

import { WorkflowNodeLibraryPanel } from "./WorkflowNodeLibraryPanel";

describe("WorkflowNodeLibraryPanel Power BI availability", () => {
  beforeEach(() => {
    catalogState.data = undefined;
  });

  test("fails closed while the catalog is unavailable or Power BI is coming soon", () => {
    const onNodeClick = vi.fn();
    const { rerender } = render(
      <WorkflowNodeLibraryPanel onNodeClick={onNodeClick} />,
    );
    expect(screen.queryByTestId("block-library-item-powerBIQuery")).toBeNull();

    catalogState.data = [{ id: "powerbi", status: "comingSoon" }];
    rerender(<WorkflowNodeLibraryPanel onNodeClick={onNodeClick} />);
    expect(screen.queryByTestId("block-library-item-powerBIQuery")).toBeNull();
    expect(onNodeClick).not.toHaveBeenCalled();
  });

  test("shows Power BI only when the served catalog marks it live", () => {
    catalogState.data = [{ id: "powerbi", status: "live" }];
    render(<WorkflowNodeLibraryPanel onNodeClick={vi.fn()} />);

    expect(screen.getByTestId("block-library-item-powerBIQuery")).toBeTruthy();
  });
});
