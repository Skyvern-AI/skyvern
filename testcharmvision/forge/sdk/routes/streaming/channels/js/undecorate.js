(function () {
  console.log("[SYS] undecorate: evaluated");

  const followers = document.querySelectorAll("#__testcharmvision_mouse_follower");

  for (const follower of followers) {
    follower.remove();
  }

  window.__testcharmvision_decoration_mouse_follower = null;
})();
