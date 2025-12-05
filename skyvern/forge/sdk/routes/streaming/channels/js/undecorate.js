(function () {
  console.log("[SYS] undecorate: evaluated");

  const followers = document.querySelectorAll("#__skyvern_mouse_follower");

  for (const follower of followers) {
    follower.remove();
  }

  window.__skyvern_decoration_mouse_follower = null;
})();
