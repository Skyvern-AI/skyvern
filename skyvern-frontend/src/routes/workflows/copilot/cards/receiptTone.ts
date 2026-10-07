export type Tone = "answered" | "neutral" | "error";

export const TONE_CLASSES: Record<
  Tone,
  { card: string; dot: string; title: string }
> = {
  answered: {
    card: "border-emerald-500/40 bg-emerald-500/[0.06]",
    dot: "bg-emerald-500 shadow-[0_0_0_3px_rgba(16,185,129,0.18)]",
    title: "text-emerald-700 dark:text-emerald-300",
  },
  neutral: {
    card: "border-slate-400/30 bg-slate-400/[0.05]",
    dot: "bg-slate-500",
    title: "text-slate-600 dark:text-slate-300",
  },
  error: {
    card: "border-rose-400/35 bg-rose-500/[0.05]",
    dot: "bg-rose-500",
    title: "text-rose-700 dark:text-rose-300",
  },
};
