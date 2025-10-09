import { Button } from "@/components/ui/button";

function WorkflowsBetaAlertCard() {
  return (
    <div className="flex flex-col items-center rounded-lg bg-slate-900 p-4 shadow">
      <header>
        <h1 className="py-4 text-3xl">Workflows (Beta)</h1>
      </header>
      <div>Workflows through UI are currently under construction.</div>
      <div>
        Today, you can create and run workflows through the Skyvern API.
      </div>
      <div className="flex gap-4 py-4">
        <Button variant="secondary" asChild>
          <a
            href="https://www.skyvern.com/docs/workflows/creating-workflows"
            target="_blank"
            rel="noopener noreferrer"
          >
            See the workflow docs
          </a>
        </Button>
        <Button asChild>
          <a
            href="https://meetings.hubspot.com/skyvern/demo"
            target="_blank"
            rel="noopener noreferrer"
          >
            Book a demo
          </a>
        </Button>
      </div>
    </div>
  );
}

export { WorkflowsBetaAlertCard };
