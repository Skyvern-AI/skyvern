import { useState } from "react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

type HeroRecordingProps = {
  recordingUrls: string[];
  onPlay?: (index: number) => void;
};

/**
 * A single recording. Once metadata loads, an empty (0-second) recording
 * shows a placeholder instead of a player that can't play anything.
 */
function RecordingPlayer({
  url,
  onPlay,
}: {
  url: string;
  onPlay?: () => void;
}) {
  const [isEmpty, setIsEmpty] = useState(false);

  // Just after a run finishes duration can be Infinity at loadedmetadata and only
  // settle later (durationchange), so check both or an empty recording shows a player.
  const checkEmpty = (event: React.SyntheticEvent<HTMLVideoElement>) => {
    const duration = event.currentTarget.duration;
    if (Number.isFinite(duration) && duration < 0.5) {
      setIsEmpty(true);
    }
  };

  if (isEmpty) {
    return (
      <div className="absolute inset-0 grid place-items-center bg-black/30 p-6 text-center text-sm text-muted-foreground">
        This recording is empty — nothing was captured (0 seconds).
      </div>
    );
  }

  return (
    <video
      src={url}
      controls
      preload="metadata"
      className="absolute inset-0 h-full w-full bg-black object-contain"
      onLoadedMetadata={checkEmpty}
      onDurationChange={checkEmpty}
      onPlay={onPlay}
    />
  );
}

/**
 * The run recording shown in the hero center. A single recording fills the
 * letterboxed area; multiple recordings get one tab each.
 */
export function HeroRecording({ recordingUrls, onPlay }: HeroRecordingProps) {
  if (recordingUrls.length === 1) {
    const url = recordingUrls[0];
    if (!url) {
      return null;
    }
    return <RecordingPlayer url={url} onPlay={() => onPlay?.(0)} />;
  }

  return (
    <Tabs
      defaultValue="0"
      className="absolute inset-0 flex flex-col bg-black/30"
    >
      <TabsList className="m-2 self-center bg-slate-elevation2">
        {recordingUrls.map((_, index) => (
          <TabsTrigger key={index} value={String(index)}>
            Recording {index + 1}
          </TabsTrigger>
        ))}
      </TabsList>
      {recordingUrls.map((url, index) => (
        <TabsContent
          key={index}
          value={String(index)}
          className="relative mt-0 min-h-0 flex-1"
        >
          <RecordingPlayer url={url} onPlay={() => onPlay?.(index)} />
        </TabsContent>
      ))}
    </Tabs>
  );
}
