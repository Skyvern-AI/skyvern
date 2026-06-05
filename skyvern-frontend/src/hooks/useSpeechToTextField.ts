import { useCallback } from "react";

import { toast } from "@/components/ui/use-toast";

import { useSpeechToText, type UseSpeechToTextResult } from "./useSpeechToText";

type UseSpeechToTextFieldOptions = {
  value: string;
  onChange: (text: string) => void;
  enabled?: boolean;
};

export function useSpeechToTextField({
  value,
  onChange,
  enabled = true,
}: UseSpeechToTextFieldOptions): UseSpeechToTextResult {
  const handleSpeechError = useCallback((message: string) => {
    toast({
      title: "Voice input failed",
      description: message,
      variant: "destructive",
    });
  }, []);

  const getBaseText = useCallback(() => value, [value]);

  return useSpeechToText({
    getBaseText,
    onTranscript: onChange,
    onError: handleSpeechError,
    enabled,
  });
}
