type Props = {
  index: number;
  description: string;
};

function NarrativeCard({ index, description }: Props) {
  return (
    <div className="flex h-32 w-52 flex-col gap-3 rounded-xl bg-slate-elevation1 p-4">
      <div className="flex size-6 items-center justify-center rounded-full border border-neutral-300 bg-neutral-200 text-xs font-semibold text-neutral-700 dark:border-neutral-700 dark:bg-neutral-800 dark:text-neutral-200">
        {index}
      </div>
      <div className="text-sm leading-5 text-muted-foreground">
        {description}
      </div>
    </div>
  );
}

export { NarrativeCard };
