import { useTheme } from "./useTheme";

function useThemeAsDarkOrLight(): "light" | "dark" {
  const { theme: baseTheme } = useTheme();

  if (baseTheme === "dark" || baseTheme === "light") {
    return baseTheme;
  }

  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export { useThemeAsDarkOrLight };
