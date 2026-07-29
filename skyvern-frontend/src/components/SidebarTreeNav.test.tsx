import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { SidebarTreeNav } from "./SidebarTreeNav";
import { useSidebarStore } from "@/store/SidebarStore";

describe("SidebarTreeNav", () => {
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    useSidebarStore.setState({ collapsed: false });
  });

  it("opens external top-level items in a new tab", () => {
    render(
      <MemoryRouter>
        <SidebarTreeNav
          items={[
            {
              label: "Home",
              to: "https://app.skyvern.com/discover",
              external: true,
              icon: <span />,
            },
          ]}
          collapsed={false}
        />
      </MemoryRouter>,
    );

    const homeLink = screen.getByRole("link", { name: "Home" });

    expect(homeLink.getAttribute("href")).toBe(
      "https://app.skyvern.com/discover",
    );
    expect(homeLink.getAttribute("target")).toBe("_blank");
    expect(homeLink.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("opens external collapsed group triggers in a new tab", () => {
    render(
      <MemoryRouter>
        <SidebarTreeNav
          items={[
            {
              label: "Agents",
              to: "https://app.skyvern.com/agents",
              external: true,
              icon: <span />,
              children: [
                {
                  label: "All Agents",
                  to: "https://app.skyvern.com/agents",
                  external: true,
                },
              ],
            },
          ]}
          collapsed
        />
      </MemoryRouter>,
    );

    const agentsTrigger = screen.getByTitle("Agents");

    expect(agentsTrigger.getAttribute("href")).toBe(
      "https://app.skyvern.com/agents",
    );
    expect(agentsTrigger.getAttribute("target")).toBe("_blank");
    expect(agentsTrigger.getAttribute("rel")).toBe("noopener noreferrer");
  });
});
