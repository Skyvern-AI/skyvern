import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SideNav } from "./SideNav";
import { useSidebarStore } from "@/store/SidebarStore";

const capture = vi.fn();
const mutate = vi.fn();

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">
      {location.pathname}
      {location.search}
    </div>
  );
}

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => false,
  usePostHog: () => ({
    capture,
  }),
}));

vi.mock("@/routes/workflows/hooks/useCreateWorkflowMutation", () => ({
  useCreateWorkflowMutation: () => ({
    isPending: false,
    mutate,
  }),
}));

describe("SideNav", () => {
  function setViewportHeight(height: number) {
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      writable: true,
      value: height,
    });
  }

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    useSidebarStore.setState({ collapsed: false });
    setViewportHeight(1024);
    mutate.mockClear();
    capture.mockClear();
  });

  it("creates a new agent from the sidebar", () => {
    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("button", { name: "New Agent" }));

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        _via: "sidebar",
        title: "New Agent",
        workflow_definition: expect.objectContaining({
          blocks: [],
          parameters: [],
        }),
      }),
    );
  });

  it("captures recipe clicks with the legacy sidebar agent event", () => {
    window.localStorage.clear();
    setViewportHeight(1024);

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link", { name: "Healthcare" }));

    expect(capture).toHaveBeenCalledWith("sidebar.agent.clicked", {
      agent: "healthcare",
      destination: "/recipes/healthcare",
      disabled: false,
      beta: true,
      badge: "Beta",
    });
  });

  it("captures recipe clicks from the collapsed sidebar menu", async () => {
    useSidebarStore.setState({ collapsed: true });

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    fireEvent.pointerDown(screen.getByTitle("Recipes"), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(
      await screen.findByRole("menuitem", { name: "Healthcare" }),
    );

    expect(capture).toHaveBeenCalledWith("sidebar.agent.clicked", {
      agent: "healthcare",
      destination: "/recipes/healthcare",
      disabled: false,
      beta: true,
      badge: "Beta",
    });
  });

  it("navigates to the parent route when clicking a collapsed group icon", () => {
    useSidebarStore.setState({ collapsed: true });

    render(
      <MemoryRouter initialEntries={["/discover"]}>
        <SideNav />
        <LocationProbe />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByTitle("Agents"));

    expect(screen.getByTestId("location").textContent).toBe("/workflows");
  });

  it("starts recipes collapsed on short screens", () => {
    window.localStorage.clear();
    setViewportHeight(860);

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("link", { name: "Healthcare" })).toBeNull();
    expect(screen.getByRole("button", { name: /Recipes/i })).toBeTruthy();
  });

  it("starts recipes expanded on tall screens", () => {
    window.localStorage.clear();
    setViewportHeight(1024);

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Healthcare" })).toBeTruthy();
  });

  it("does not duplicate expanded group labels inside their children", () => {
    window.localStorage.setItem(
      "skyvern-sidebar-open-groups",
      JSON.stringify({ "/recipes": true, "/credentials": true }),
    );
    setViewportHeight(1024);

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    expect(screen.getAllByText("Recipes")).toHaveLength(1);
    expect(screen.getAllByText("Credentials")).toHaveLength(1);
  });

  it("starts integrations collapsed by default", () => {
    window.localStorage.clear();
    setViewportHeight(1024);

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("link", { name: "MCP" })).toBeNull();
    expect(screen.getByRole("button", { name: /Integrations/i })).toBeTruthy();
  });

  it("preserves a stored recipes state on short screens", () => {
    window.localStorage.setItem(
      "skyvern-sidebar-open-groups",
      JSON.stringify({ Recipes: true }),
    );
    setViewportHeight(860);

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Healthcare" })).toBeTruthy();
  });

  it("uses route keys for persisted group state without rewriting storage on mount", () => {
    const storedState = {
      "/recipes": true,
      "/future-section": false,
    };
    window.localStorage.setItem(
      "skyvern-sidebar-open-groups",
      JSON.stringify(storedState),
    );
    setViewportHeight(860);

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: "Healthcare" })).toBeTruthy();
    expect(
      JSON.parse(
        window.localStorage.getItem("skyvern-sidebar-open-groups") ?? "{}",
      ),
    ).toEqual(storedState);
  });

  it("renders expanded navigation when collapsed override is false despite store", () => {
    useSidebarStore.setState({ collapsed: true });
    setViewportHeight(1024);

    render(
      <MemoryRouter>
        <SideNav collapsed={false} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("button", { name: /Agents/i })).toBeTruthy();
    expect(screen.queryByTitle("Agents")).toBeNull();
  });

  it("shows clickable parent headers in collapsed popout menus", async () => {
    useSidebarStore.setState({ collapsed: true });

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    fireEvent.pointerDown(screen.getByTitle("Agents"), {
      button: 0,
      ctrlKey: false,
    });

    expect(
      await screen.findByRole("menuitem", { name: "All Agents" }),
    ).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "Agents" })).toBeTruthy();
  });

  it("uses the n8n logo in the collapsed integrations popout", async () => {
    useSidebarStore.setState({ collapsed: true });

    render(
      <MemoryRouter>
        <SideNav />
      </MemoryRouter>,
    );

    fireEvent.pointerDown(screen.getByTitle("Integrations"), {
      button: 0,
      ctrlKey: false,
    });

    const n8nMenuItem = await screen.findByRole("menuitem", { name: "n8n" });
    expect(
      n8nMenuItem.querySelector('svg[viewBox="0 0 304 160"]'),
    ).toBeTruthy();
  });
});
