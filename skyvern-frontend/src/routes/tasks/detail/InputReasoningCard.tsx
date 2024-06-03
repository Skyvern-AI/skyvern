type Props = {
  input: string;
  reasoning: string;
  confidence: number;
};

function InputReasoningCard({ input, reasoning, confidence }: Props) {
  return (
    <div className="flex p-4 gap-2 rounded-md shadow-md border items-start">
      <div className="flex-1">
        <div className="text-sm">
          <span className="font-bold">Agent Input:</span> {input}
        </div>
        <div className="text-sm">
          <span className="font-bold">Agent Reasoning:</span> {reasoning}
        </div>
      </div>
      <div className="flex items-center justify-center border p-2 rounded-lg bg-muted">
        <span>Confidence: {confidence}</span>
      </div>
    </div>
  );
}

export { InputReasoningCard };
