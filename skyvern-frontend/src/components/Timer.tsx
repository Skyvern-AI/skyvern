import { useEffect, useState } from "react";

interface HMS {
  hour: number;
  minute: number;
  second: number;
}

interface Props {
  startAt?: HMS;
}

function Timer({ startAt }: Props) {
  const [time, setTime] = useState<HMS>({
    hour: 0,
    minute: 0,
    second: 0,
  });

  useEffect(() => {
    const start = performance.now();

    const loop = () => {
      const elapsed = performance.now() - start;
      let seconds = Math.floor(elapsed / 1000);
      let minutes = Math.floor(seconds / 60);
      let hours = Math.floor(minutes / 60);
      seconds = seconds % 60;
      minutes = minutes % 60;
      hours = hours % 24;
      setTime(() => ({
        hour: hours + (startAt?.hour ?? 0),
        minute: minutes + (startAt?.minute ?? 0),
        second: seconds + (startAt?.second ?? 0),
      }));

      rAF = requestAnimationFrame(loop);
    };

    let rAF = requestAnimationFrame(loop);

    return () => cancelAnimationFrame(rAF);
  }, [startAt]);

  return (
    <div>
      {String(time.hour).padStart(2, "0")}:
      {String(time.minute).padStart(2, "0")}:
      {String(time.second).padStart(2, "0")}
    </div>
  );
}

export { Timer };
