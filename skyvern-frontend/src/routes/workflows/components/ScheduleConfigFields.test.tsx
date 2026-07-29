import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScheduleConfigFields } from "./ScheduleConfigFields";

function Harness({ initialCron }: { initialCron: string }) {
  const [cron, setCron] = useState(initialCron);
  const [timezone, setTimezone] = useState("UTC");
  return (
    <>
      <ScheduleConfigFields
        cronExpression={cron}
        timezone={timezone}
        onCronChange={setCron}
        onTimezoneChange={setTimezone}
      />
      <output data-testid="cron-out">{cron}</output>
    </>
  );
}

function cronOut() {
  return screen.getByTestId("cron-out").textContent;
}

function pressed(name: string) {
  return screen.getByRole("button", { name }).getAttribute("aria-pressed");
}

describe("ScheduleConfigFields", () => {
  it("seeds the day-of-week chips from a weekdays cron", () => {
    render(<Harness initialCron="0 9 * * 1-5" />);
    for (const day of [
      "Monday",
      "Tuesday",
      "Wednesday",
      "Thursday",
      "Friday",
    ]) {
      expect(pressed(day)).toBe("true");
    }
    for (const day of ["Sunday", "Saturday"]) {
      expect(pressed(day)).toBe("false");
    }
  });

  it("toggling a day updates the generated cron", () => {
    render(<Harness initialCron="0 9 * * 1-5" />);
    fireEvent.click(screen.getByRole("button", { name: "Saturday" }));
    expect(cronOut()).toBe("0 9 * * 1,2,3,4,5,6");
  });

  it("keeps at least one weekly day selected", () => {
    render(<Harness initialCron="0 9 * * 1" />);
    fireEvent.click(screen.getByRole("button", { name: "Monday" }));
    expect(pressed("Monday")).toBe("true");
    expect(cronOut()).toBe("0 9 * * 1");
  });

  it("treats an unsupported expression as custom and surfaces the raw cron", () => {
    render(<Harness initialCron="*/1 * * * *" />);
    // Day pickers only render for a recognized weekly schedule; a custom
    // expression hides them and drops the user into the advanced field.
    expect(screen.queryByRole("button", { name: "Monday" })).toBeNull();
    const input = screen.getByPlaceholderText("* * * * *") as HTMLInputElement;
    expect(input.value).toBe("*/1 * * * *");
    expect(screen.queryByText(/at least 5 minutes apart/i)).not.toBeNull();
  });

  it("propagates edits made in the advanced cron field", () => {
    render(<Harness initialCron="0 9 * * *" />);
    fireEvent.click(
      screen.getByRole("button", { name: /advanced \(cron expression\)/i }),
    );
    const input = screen.getByPlaceholderText("* * * * *");
    fireEvent.change(input, { target: { value: "0 14 * * *" } });
    expect(cronOut()).toBe("0 14 * * *");
  });
});
