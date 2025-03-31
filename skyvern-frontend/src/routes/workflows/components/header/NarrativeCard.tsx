type Props = {
  index: number;
  description: string;
};

function NarrativeCard({ index, description }: Props) {
  return (
    <div className="flex h-32 w-52 flex-col gap-3 rounded-xl bg-slate-elevation1 p-4">
      <div className="flex size-6 items-center justify-center rounded-full bg-slate-400 text-slate-950">
        {index}
      </div>
      <div className="text-sm text-slate-300">{description}</div>
    </div>
  );
}

export { NarrativeCard };
