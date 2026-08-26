interface AnimatedWaveProps {
  text: string;
  className?: string;
}

export function AnimatedWave({ text, className = "" }: AnimatedWaveProps) {
  const characters = text.split("");

  return (
    <>
      <style>{`
        @keyframes wave {
          0%, 100% {
            transform: translateY(0px);
          }
          50% {
            transform: translateY(-4px);
          }
        }
        .animate-wave {
          animation-name: wave;
        }
        @media (prefers-reduced-motion: reduce) {
          /* animation-name is the only animation property not set inline, so
             clearing it here is what actually stops the wave. */
          .animate-wave {
            animation-name: none;
          }
        }
      `}</style>
      <span className={`inline-flex ${className}`}>
        {characters.map((char, index) => (
          <span
            key={index}
            className="animate-wave inline-block"
            style={{
              animationDelay: `${index * 0.1}s`,
              animationDuration: "1.3s",
              animationIterationCount: "infinite",
              animationTimingFunction: "ease-in-out",
            }}
          >
            {char === " " ? "\u00A0" : char}
          </span>
        ))}
      </span>
    </>
  );
}
