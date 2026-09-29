// A card's 1px border plus px-3 puts its content 13px in, so the turn's other rows take the same
// inset and every glyph and text edge in a turn lines up. A margin, so it composes with row padding.
export const TURN_ROW_INSET = "mx-[13px]";
export const ICON_COLUMN = "flex w-[18px] shrink-0 justify-center";

export const ACCEPT_BUTTON_CLASS =
  "bg-success font-semibold text-success-foreground hover:bg-success/90";
export const REJECT_BUTTON_CLASS =
  "border-red-500/40 text-red-700 hover:bg-red-500/10 hover:text-red-700 dark:text-red-400 dark:hover:text-red-400";
