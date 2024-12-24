type Props = {
  recipients: Array<string>;
  body: string;
};

function SendEmailBlockInfo({ recipients, body }: Props) {
  return (
    <div className="flex gap-2">
      <div className="w-1/2 space-y-4 p-4">
        <div className="flex justify-between">
          <span className="text-sm text-slate-400">From</span>
          <span className="text-sm">hello@skyvern.com</span>
        </div>
        <div className="flex justify-between">
          <span className="text-sm text-slate-400">To</span>
          {recipients.map((recipient) => {
            return <span className="text-sm">{recipient}</span>;
          })}
        </div>
      </div>
      <div className="w-1/2 space-y-4 p-4">
        <span className="text-sm text-slate-400">Body</span>
        <p className="text-sm">{body}</p>
      </div>
    </div>
  );
}

export { SendEmailBlockInfo };
