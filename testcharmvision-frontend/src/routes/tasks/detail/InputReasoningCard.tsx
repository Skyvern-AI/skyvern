type Props = {
  input: string;
  reasoning: string;
  confidence: number;
};

function InputReasoningCard({ input, reasoning, confidence }: Props) {
  return (
    <div className="flex items-start gap-2 rounded-md border p-4 shadow-md">
      <div className="flex-1">
        <div className="text-sm">
          <span className="font-bold">Agent Input:</span> {input}
        </div>
        <div className="text-sm">
          <span className="font-bold">Agent Reasoning:</span> {reasoning}
        </div>
      </div>
      <div className="flex items-center justify-center rounded-lg border bg-muted p-2">
        <span>Confidence: {confidence}</span>
      </div>
    </div>
  );
}

export { InputReasoningCard };
