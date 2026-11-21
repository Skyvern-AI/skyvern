import { useFeatureFlag } from "@/hooks/useFeatureFlag";

const GOOGLE_DRIVE_FOLDER_HELP =
  "Destination Google Drive folder. You can paste a Drive folder URL or a bare folder ID. Leave empty to upload to the account's My Drive root.";
const GOOGLE_SHARED_DRIVE_FOLDER_HELP =
  "Destination Google Drive folder. You can paste a Drive folder URL, including a shared drive folder, or a bare folder ID. Leave empty to upload to the account's My Drive root.";

export function useGoogleDriveFolderHelp(): string {
  return useFeatureFlag("DRIVE_SHARED_DRIVE_SEARCH") === true
    ? GOOGLE_SHARED_DRIVE_FOLDER_HELP
    : GOOGLE_DRIVE_FOLDER_HELP;
}
