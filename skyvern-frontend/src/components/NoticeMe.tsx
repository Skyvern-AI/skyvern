import { useEffect, useRef, useState } from "react";

interface Props {
  trigger: "render" | "viewport";
}

function NoticeMe({ children, trigger }: React.PropsWithChildren<Props>) {
  const [shouldAnimate, setShouldAnimate] = useState(trigger === "render");
  const [shouldHide, setShouldHide] = useState(false);
  const elementRef = useRef<HTMLDivElement>(null);
  const hasExitedRef = useRef(false);

  useEffect(() => {
    if (trigger !== "viewport") return;

    const element = elementRef.current;
    if (!element) return;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            // Element is visible in viewport
            if (hasExitedRef.current) {
              // Force animation restart by removing then re-adding class
              setShouldHide(false);
              setShouldAnimate(false);
              // Use setTimeout to ensure the class is removed before re-adding
              setTimeout(() => {
                setShouldAnimate(true);
              }, 10);
              hasExitedRef.current = false;
            }
          } else {
            // Element is NOT visible in viewport (completely outside)
            setShouldAnimate(false);
            setShouldHide(true);
            hasExitedRef.current = true;
          }
        });
      },
      {
        threshold: 0,
        rootMargin: "0px",
      },
    );

    observer.observe(element);

    return () => observer.disconnect();
  }, [trigger]);

  const getAnimationClass = () => {
    if (shouldHide) return "notice-me-hidden";
    if (shouldAnimate) return "notice-me-animate";
    return "";
  };

  return (
    <>
      <div
        ref={elementRef}
        className={getAnimationClass()}
        style={{ display: "flex" }}
      >
        {children}
      </div>
      <style>{`
        .notice-me-hidden {
          opacity: 0;
        }

        .notice-me-animate {
          animation: notice-fade-up 0.5s ease-out;
        }

        @keyframes notice-fade-up {
          0% {
            opacity: 0;
            transform: translateY(20px);
          }
          100% {
            opacity: 1;
            transform: translateY(0);
          }
        }
      `}</style>
    </>
  );
}

export { NoticeMe };
