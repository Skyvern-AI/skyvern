import { Button } from "@/components/ui/button";

function WorkflowsBetaAlertCard() {
  return (
    <div className="shadow rounded-lg bg-slate-900 flex flex-col items-center p-4">
      <header>
        <h1 className="text-3xl py-4">Workflows (Beta)</h1>
      </header>
      <div>Workflows through UI are currently under construction.</div>
      <div>
        Today, you can create and run workflows through the Skyvern API.
      </div>
      <div className="flex gap-4 py-4">
        <Button variant="secondary" asChild>
          <a
            href="https://docs.skyvern.com/workflows/creating-workflows"
            target="_blank"
            rel="noopener noreferrer"
          >
            See the workflow docs
          </a>
        </Button>
        <Button asChild>
          <a
            href="https://meetings.hubspot.com/suchintan"
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
