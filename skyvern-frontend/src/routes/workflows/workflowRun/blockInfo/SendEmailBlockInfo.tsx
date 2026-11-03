import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import {
  gmailOutcomeTitles,
  type GmailSendOutput,
} from "@/routes/workflows/types/workflowRunTypes";

type Props = {
  recipients: Array<string>;
  body: string;
  subject: string;
};

function SendEmailBlockParameters({ recipients, body, subject }: Props) {
  return (
    <div className="space-y-4">
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">To</h1>
        </div>
        <Input value={recipients.join(", ")} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Subject</h1>
        </div>
        <Input value={subject} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Body</h1>
        </div>
        <AutoResizingTextarea value={body} readOnly />
      </div>
    </div>
  );
}

function GmailSendOutcome({
  output,
  failureReason,
}: {
  output: GmailSendOutput;
  failureReason: string | null;
}) {
  return (
    <div className="space-y-2" data-testid="gmail-send-outcome">
      <h1 className="text-sm font-bold">
        {gmailOutcomeTitles[output.outcome]}
      </h1>
      {output.outcome === "accepted" ? (
        <p className="text-sm text-muted-foreground">
          Gmail message ID: {output.provider_message_id}
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">{failureReason}</p>
      )}
      {output.replayed ? (
        <p className="text-xs text-muted-foreground">
          This is the result recorded by an earlier attempt of the same step;
          the message was not sent again.
        </p>
      ) : null}
    </div>
  );
}

export { GmailSendOutcome, SendEmailBlockParameters };
