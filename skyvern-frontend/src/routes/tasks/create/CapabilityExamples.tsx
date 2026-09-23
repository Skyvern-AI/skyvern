import {
  CalendarIcon,
  ClockIcon,
  CodeIcon,
  DownloadIcon,
  EnvelopeClosedIcon,
  FileTextIcon,
  LockClosedIcon,
  TableIcon,
} from "@radix-ui/react-icons";
import type { ReactNode } from "react";

import { GraphIcon } from "@/components/icons/GraphIcon";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import "./capabilityExamples.css";

type Capability = "forms" | "extract" | "login" | "monitor" | "schedule";

type Example = { id: string; label: string; prompt: string; icon: ReactNode };

type Group = {
  capability: Capability;
  label: string;
  caption: string;
  preview: ReactNode;
  examples: [Example, Example];
};

const TruckIcon = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
    <path d="M3 7h11v9H3zM14 10h4l3 3v3h-7z" />
    <circle cx="7" cy="18" r="1.6" />
    <circle cx="17" cy="18" r="1.6" />
  </svg>
);

function Frame({ kind, children }: { kind: string; children: ReactNode }) {
  return (
    <div className={`cx-frame ${kind}`} aria-hidden="true">
      <div className="cx-bar">
        <span className="cx-dot" />
        <span className="cx-dot" />
        <span className="cx-dot" />
        <span className="cx-url" />
      </div>
      <div className="cx-body">{children}</div>
    </div>
  );
}

const GROUPS: ReadonlyArray<Group> = [
  {
    capability: "forms",
    label: "Fill forms",
    caption: "Skyvern types into each field, then submits the form.",
    preview: (
      <Frame kind="cx-form">
        {[0, 1, 2].map((i) => (
          <div key={i} className="cx-row">
            <span className="cx-lab" />
            <span className="cx-field">
              <span className="cx-ink" />
            </span>
          </div>
        ))}
        <div className="cx-row" style={{ margin: 0 }}>
          <span className="cx-btn">Submit</span>
        </div>
      </Frame>
    ),
    examples: [
      {
        id: "forms.apply_for_job",
        label: "Apply for a job",
        icon: <EnvelopeClosedIcon />,
        prompt:
          "Go to https://jobs.lever.co/leverdemo-8, find the first Solutions Engineer role in Toronto, and apply with the attached resume. Fill in the name and email, submit, and confirm the application went through.",
      },
      {
        id: "forms.get_quote",
        label: "Get a quote",
        icon: <FileTextIcon />,
        prompt:
          "Go to https://www.geico.com and generate an auto insurance quote for a 2015 Audi A8 L in Houston, TX. Stop once a premium amount is shown and extract the quote details as JSON.",
      },
    ],
  },
  {
    capability: "extract",
    label: "Extract data",
    caption: "Skyvern reads the rows on the page and returns structured JSON.",
    preview: (
      <Frame kind="cx-table">
        <div className="cx-split">
          <div className="cx-rows">
            <span className="cx-trow" />
            <span className="cx-trow" />
            <span className="cx-trow" />
            <span className="cx-trow" />
          </div>
          <div className="cx-json">
            {"[{\n "}
            <b>&quot;sku&quot;</b>
            {':"AX-14",\n '}
            <b>&quot;price&quot;</b>
            {":129,\n "}
            <b>&quot;stock&quot;</b>
            {":8\n}]"}
          </div>
        </div>
      </Frame>
    ),
    examples: [
      {
        id: "extract.scrape_catalog",
        label: "Scrape a catalog",
        icon: <TableIcon />,
        prompt:
          "Go to the vendor's product listing, page through every result, and collect the SKU, name, price, and stock level for each item.",
      },
      {
        id: "extract.extract_to_json",
        label: "Extract to JSON",
        icon: <CodeIcon />,
        prompt:
          "Open the order confirmation page, extract the order number, line items, and total, and return it as JSON.",
      },
    ],
  },
  {
    capability: "login",
    label: "Log in and download",
    caption: "Skyvern signs in, reads the 2FA code, and saves the file.",
    preview: (
      <Frame kind="cx-login">
        <div className="cx-row">
          <span className="cx-lab" />
          <span className="cx-field">
            <span className="cx-ink" style={{ width: "64%" }} />
          </span>
        </div>
        <div className="cx-row">
          <span className="cx-lab" />
          <span className="cx-field">
            <span className="cx-dots">
              <i />
              <i />
              <i />
              <i />
              <i />
              <i />
            </span>
          </span>
        </div>
        <span className="cx-otp">
          <LockClosedIcon />
          482 193
        </span>
        <div>
          <span className="cx-file">
            <DownloadIcon />
            invoices-may.pdf
          </span>
        </div>
      </Frame>
    ),
    examples: [
      {
        id: "login.download_invoices",
        label: "Download invoices",
        icon: <DownloadIcon />,
        prompt:
          "Log in to the supplier portal with the saved credentials and download every invoice from last month as PDF.",
      },
      {
        id: "login.export_report",
        label: "Export a report",
        icon: <LockClosedIcon />,
        prompt:
          "Log in to the payroll system, run the month-end report, and download it as CSV.",
      },
    ],
  },
  {
    capability: "monitor",
    label: "Monitor",
    caption: "Skyvern re-checks the page and alerts you when the value moves.",
    preview: (
      <Frame kind="cx-watch">
        <div className="cx-price">
          <span className="cx-p-old">$1,299</span>
          <span className="cx-p-new">$1,149</span>
        </div>
        <div className="cx-meta">Pro plan, checked every morning</div>
        <svg className="cx-spark" viewBox="0 0 150 34" width="150" height="34">
          <path
            className="cx-line"
            d="M2 10 L26 8 L50 13 L74 9 L98 12 L122 24 L146 26"
          />
          <circle className="cx-tip" cx="146" cy="26" r="2.6" />
        </svg>
        <span className="cx-bell">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
          >
            <path d="M6 16V11a6 6 0 1 1 12 0v5l2 3H4zM10 22h4" />
          </svg>
        </span>
      </Frame>
    ),
    examples: [
      {
        id: "monitor.watch_price",
        label: "Watch a price",
        icon: <GraphIcon className="size-[22px]" />,
        prompt:
          "Check the competitor's pricing page every morning and message me when the Pro plan price changes.",
      },
      {
        id: "monitor.track_shipment",
        label: "Track a shipment",
        icon: TruckIcon,
        prompt:
          "Log in to the carrier site, look up tracking number 1Z999AA10123456784, and report the current status and delivery date.",
      },
    ],
  },
  {
    capability: "schedule",
    label: "Run on a schedule",
    caption: "Once it works, schedule it to run again from the agent page.",
    preview: (
      <Frame kind="cx-cal">
        <div className="cx-grid">
          <span className="cx-ring" />
          {Array.from({ length: 21 }, (_, i) => (
            <i key={i} />
          ))}
        </div>
        <span className="cx-when">Every Tuesday, 08:00</span>
        <svg className="cx-clock" viewBox="0 0 34 34">
          <circle cx="17" cy="17" r="12" />
          <path className="cx-hand" d="M17 17V8" />
        </svg>
      </Frame>
    ),
    examples: [
      {
        id: "schedule.weekly_summary",
        label: "Weekly summary",
        icon: <CalendarIcon />,
        prompt:
          "Log in to the analytics dashboard, download last week's report, and email it to the team.",
      },
      {
        id: "schedule.daily_sync",
        label: "Daily sync",
        icon: <ClockIcon />,
        prompt:
          "Pull today's inventory counts from the warehouse portal and append them to the tracking sheet.",
      },
    ],
  },
];

// Three groups on top, two centered beneath, at every width.
const GROUP_ROWS = [GROUPS.slice(0, 3), GROUPS.slice(3)];

type Selection = {
  id: string;
  capability: Capability;
  label: string;
  prompt: string;
};

type Props = {
  disabled?: boolean;
  onSelect: (selection: Selection) => void;
  onPreview?: (selection: Omit<Selection, "prompt">) => void;
};

function CapabilityExamples({ disabled = false, onSelect, onPreview }: Props) {
  return (
    <TooltipProvider delayDuration={120}>
      <div className="flex w-full flex-col items-center gap-5">
        {GROUP_ROWS.map((row, rowIndex) => (
          <div
            key={rowIndex}
            className="flex flex-wrap items-start justify-center gap-x-[18px] gap-y-5"
          >
            {row.map((group) => (
              <div
                key={group.capability}
                className="flex w-[196px] flex-col gap-2"
              >
                <div className="pl-0.5 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
                  {group.label}
                </div>
                {group.examples.map((example) => (
                  <Tooltip
                    key={example.label}
                    onOpenChange={(open) => {
                      if (open) {
                        onPreview?.({
                          id: example.id,
                          capability: group.capability,
                          label: example.label,
                        });
                      }
                    }}
                  >
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        disabled={disabled}
                        onClick={() =>
                          onSelect({
                            id: example.id,
                            capability: group.capability,
                            label: example.label,
                            prompt: example.prompt,
                          })
                        }
                        className="flex h-12 w-full items-center gap-2.5 rounded-[10px] border border-transparent bg-slate-elevation2 pl-4 pr-5 text-left text-sm text-foreground transition-colors hover:border-border hover:bg-slate-elevation3 disabled:pointer-events-none disabled:opacity-50 [&>svg]:size-[22px] [&>svg]:shrink-0"
                      >
                        {example.icon}
                        <span className="truncate">{example.label}</span>
                      </button>
                    </TooltipTrigger>
                    <TooltipContent
                      side="top"
                      sideOffset={12}
                      className="w-[260px] whitespace-normal rounded-xl border border-border bg-slate-elevation1 p-2.5 text-foreground shadow-2xl"
                    >
                      {group.preview}
                      <p className="mx-0.5 mt-2 text-[11.5px] leading-snug text-muted-foreground">
                        {group.caption}
                      </p>
                    </TooltipContent>
                  </Tooltip>
                ))}
              </div>
            ))}
          </div>
        ))}
      </div>
    </TooltipProvider>
  );
}

export { CapabilityExamples };
