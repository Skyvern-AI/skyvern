import { cva } from "class-variance-authority";

/** Default variant preserves pre-cva Table output. `line-5col` is the dashboard breakdown schema: Workflow / Runs / Success% / Avg Cost / Total Cost. */
const tableVariants = cva("w-full caption-bottom text-sm", {
  variants: {
    variant: {
      default: "",
      "line-5col": [
        // Right-align cols 2-5 (Runs, Success%, Avg Cost, Total Cost).
        // Col 1 (Workflow) keeps the default left alignment.
        "[&_th:nth-child(n+2)]:text-right",
        "[&_td:nth-child(n+2)]:text-right",
        // Tabular-nums on numeric cells so 1,234 lines up under 12,345.
        "[&_td:nth-child(n+2)]:tabular-nums",
        // Fixed widths: Runs is narrower (80px) than the cost cols (96px).
        // Workflow col (1) absorbs the remaining width via the default
        // `w-auto` behavior of <th>.
        "[&_th:nth-child(2)]:w-20",
        "[&_th:nth-child(3)]:w-24",
        "[&_th:nth-child(4)]:w-24",
        "[&_th:nth-child(5)]:w-24",
        // Total Cost is the bottom-line number — bold so the eye lands
        // there first when scanning the breakdown.
        "[&_td:nth-child(5)]:font-semibold",
      ].join(" "),
    },
  },
  defaultVariants: {
    variant: "default",
  },
});

export { tableVariants };
