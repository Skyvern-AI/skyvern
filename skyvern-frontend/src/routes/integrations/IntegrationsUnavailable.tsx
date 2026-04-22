import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

function IntegrationsUnavailable() {
  return (
    <div className="mx-auto max-w-2xl p-6">
      <Card>
        <CardHeader>
          <CardTitle>Integrations</CardTitle>
          <CardDescription>
            Third-party integrations are available in Skyvern Cloud.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-slate-400">
          <p>
            Connect your Google account and other providers by signing up at{" "}
            <a
              href="https://app.skyvern.com"
              target="_blank"
              rel="noopener noreferrer"
              className="text-slate-200 underline hover:text-slate-100"
            >
              app.skyvern.com
            </a>
            .
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

export { IntegrationsUnavailable };
