import { ReactNode, useEffect, useState } from "react";

interface RotateThroughProps {
  children: ReactNode[];
  interval: number; // milliseconds
  className?: string;
}

function RotateThrough({
  children,
  interval,
  className = "",
}: RotateThroughProps) {
  const [currentIndex, setCurrentIndex] = useState(0);
  const [isAnimating, setIsAnimating] = useState(false);

  const childrenArray = Array.isArray(children) ? children : [children];

  useEffect(() => {
    if (childrenArray.length <= 1) return;

    const timer = setInterval(() => {
      setIsAnimating(true);

      // After a short animation delay, change the content
      setTimeout(() => {
        setCurrentIndex((prevIndex) =>
          prevIndex === childrenArray.length - 1 ? 0 : prevIndex + 1,
        );
        setIsAnimating(false);
      }, 150); // Animation duration
    }, interval);

    return () => clearInterval(timer);
  }, [childrenArray.length, interval]);

  if (childrenArray.length === 0) {
    return null;
  }

  if (childrenArray.length === 1) {
    return <div className={className}>{childrenArray[0]}</div>;
  }

  return (
    <div
      className={`transition-all duration-150 ease-in-out ${
        isAnimating ? "scale-95 opacity-0" : "scale-100 opacity-100"
      } ${className}`}
    >
      {childrenArray[currentIndex]}
    </div>
  );
}

export { RotateThrough };
