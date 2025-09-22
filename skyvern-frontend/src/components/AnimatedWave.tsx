interface AnimatedWaveProps {
  text: string;
  className?: string;
  duration?: string;
  waveHeight?: string;
}

export function AnimatedWave({
  text,
  className = "",
  duration = "1.3s",
  waveHeight = "4px",
}: AnimatedWaveProps) {
  const characters = text.split("");

  return (
    <>
      <style>{`
        @keyframes wave {
          0%, 100% {
            transform: translateY(0px);
          }
          50% {
            transform: translateY(-${waveHeight});
          }
        }
        .animate-wave {
          animation-name: wave;
        }
      `}</style>
      <span className={`inline-flex ${className}`}>
        {characters.map((char, index) => (
          <span
            key={index}
            className="animate-wave inline-block"
            style={{
              animationDelay: `${index * 0.1}s`,
              animationDuration: duration,
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
