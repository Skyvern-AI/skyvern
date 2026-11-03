type Props = {
  text: string;
  // True until the running turn hands the message to the model.
  sending: boolean;
};

export function SteerReceipt({ text, sending }: Props) {
  return (
    <div data-testid="copilot-steer-receipt" className="flex justify-end">
      <div className="max-w-[85%] rounded-xl border border-white/5 bg-slate-elevation4 px-3.5 py-2.5 text-[13.5px] leading-[1.5] text-foreground">
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{text}</p>
        <p role="status" className="mt-1 text-[11px] text-muted-foreground">
          {sending ? "Sending now…" : "Sent while Copilot was working"}
        </p>
      </div>
    </div>
  );
}
