// Below this height, the expanded recipe preview pushes Settings out of the
// sidebar viewport on a fresh session.
const SIDEBAR_RECIPES_DEFAULT_OPEN_MIN_HEIGHT = 920;

function shouldDefaultRecipesOpen() {
  try {
    if (typeof window === "undefined") {
      return true;
    }
    return window.innerHeight >= SIDEBAR_RECIPES_DEFAULT_OPEN_MIN_HEIGHT;
  } catch {
    return true;
  }
}

export { shouldDefaultRecipesOpen };
