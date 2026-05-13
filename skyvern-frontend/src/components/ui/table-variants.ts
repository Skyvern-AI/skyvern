import { cva } from "class-variance-authority";

/** Default variant preserves pre-cva Table output. `line-5col` is the dashboard breakdown schema: Workflow / Runs / Success% / Avg Cost / Total Cost. */
const tableVariants = cva("w-full caption-bottom text-sm", {
  variants: {
    variant: {
      default: "",
      "line-5col": [
        "[&_th:nth-child(n+2)]:text-right",
        "[&_td:nth-child(n+2)]:text-right",
        "[&_td:nth-child(n+2)]:tabular-nums",
        "[&_th:nth-child(2)]:w-20",
        "[&_th:nth-child(3)]:w-24",
        "[&_th:nth-child(4)]:w-24",
        "[&_th:nth-child(5)]:w-24",
        "[&_td:nth-child(5)]:font-semibold",
      ].join(" "),
    },
  },
  defaultVariants: {
    variant: "default",
  },
});

export { tableVariants };
