import { useEffect, useState } from "react";

import { formatMs } from "@/util/utils";

interface HMS {
  hour: number;
  minute: number;
  second: number;
}

interface Props {
  override?: number;
  startAt?: HMS;
}

function Timer({ override, startAt }: Props) {
  const [time, setTime] = useState<HMS>({
    hour: 0,
    minute: 0,
    second: 0,
  });

  useEffect(() => {
    if (override) {
      const formatted = formatMs(override);
      setTime(() => formatted);

      return;
    }

    const start = performance.now();

    const loop = () => {
      const elapsed = performance.now() - start;
      const formatted = formatMs(elapsed);
      setTime(() => ({
        hour: formatted.hour + (startAt?.hour ?? 0),
        minute: formatted.minute + (startAt?.minute ?? 0),
        second: formatted.second + (startAt?.second ?? 0),
      }));

      rAF = requestAnimationFrame(loop);
    };

    let rAF = requestAnimationFrame(loop);

    return () => cancelAnimationFrame(rAF);
  }, [override, startAt]);

  return (
    <div>
      {String(time.hour).padStart(2, "0")}:
      {String(time.minute).padStart(2, "0")}:
      {String(time.second).padStart(2, "0")}
    </div>
  );
}

export { Timer };
