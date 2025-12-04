(function () {
  console.log("[SYS] decorate: evaluated");

  function initiate() {
    if (!window.__skyvern_decoration_initialized) {
      console.log("[SYS] decorate: initializing");

      window.__skyvern_decoration_initialized = true;

      window.__skyvern_create_mouse_follower = function () {
        const preexistingCircles = document.querySelectorAll(
          "#__skyvern_mouse_follower",
        );

        if (preexistingCircles.length > 0) {
          for (const circle of preexistingCircles) {
            circle.remove();
          }
        }

        const circle = document.createElement("div");
        window.__skyvern_decoration_mouse_follower = circle;
        circle.id = "__skyvern_mouse_follower";
        circle.style.position = "fixed";
        circle.style.left = "0";
        circle.style.top = "0";
        circle.style.width = "30px";
        circle.style.height = "30px";
        circle.style.borderRadius = "50%";
        circle.style.backgroundColor = "rgba(255, 0, 0, 0.2)";
        circle.style.pointerEvents = "none";
        circle.style.zIndex = "999999";
        circle.style.willChange = "transform";
        document.body.appendChild(circle);
      };

      window.__skyvern_create_mouse_follower();

      let scale = 1;
      let targetScale = 1;
      let mouseX = 0;
      let mouseY = 0;

      // smooth scale animation
      function animate() {
        if (!window.__skyvern_decoration_mouse_follower) {
          return;
        }

        const follower = window.__skyvern_decoration_mouse_follower;

        scale += (targetScale - scale) * 0.2;

        if (Math.abs(targetScale - scale) > 0.001) {
          requestAnimationFrame(animate);
        } else {
          scale = targetScale;
        }

        follower.style.transform = `translate(${mouseX - 15}px, ${mouseY - 15}px) scale(${scale})`;
      }

      // update follower position on mouse move
      document.addEventListener(
        "mousemove",
        (e) => {
          if (!window.__skyvern_decoration_mouse_follower) {
            return;
          }

          const follower = window.__skyvern_decoration_mouse_follower;
          mouseX = e.clientX;
          mouseY = e.clientY;
          follower.style.transform = `translate(${mouseX - 15}px, ${mouseY - 15}px) scale(${scale})`;
        },
        true,
      );

      // expand follower on mouse down
      document.addEventListener(
        "mousedown",
        () => {
          if (!window.__skyvern_decoration_mouse_follower) {
            return;
          }

          targetScale = 50 / 30;
          requestAnimationFrame(animate);
        },
        true,
      );

      // return follower to original size on mouse up
      document.addEventListener(
        "mouseup",
        () => {
          if (!window.__skyvern_decoration_mouse_follower) {
            return;
          }

          targetScale = 1;
          requestAnimationFrame(animate);
        },
        true,
      );
    } else {
      window.__skyvern_create_mouse_follower();
    }
  }

  if (document.body) {
    console.log("[SYS] decorate: document already loaded, initiating");
    initiate();
  } else {
    console.log("[SYS] decorate: waiting for DOMContentLoaded to initiate");
    document.addEventListener("DOMContentLoaded", initiate);
  }
})();
