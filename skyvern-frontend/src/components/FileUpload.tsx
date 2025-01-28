import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import { Cross2Icon, FileIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useMutation } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { toast } from "./ui/use-toast";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";

export type FileInputValue =
  | {
      s3uri: string;
      presignedUrl: string;
    }
  | string
  | null;

type Props = {
  value: FileInputValue;
  onChange: (value: FileInputValue) => void;
};

const FILE_SIZE_LIMIT_IN_BYTES = 10 * 1024 * 1024; // 10 MB

function showFileSizeError() {
  toast({
    variant: "destructive",
    title: "File size limit exceeded",
    description:
      "The file you are trying to upload exceeds the 10MB limit, please try again with a different file",
  });
}

function FileUpload({ value, onChange }: Props) {
  const credentialGetter = useCredentialGetter();
  const [file, setFile] = useState<File | null>(null);
  const inputId = useId();

  const uploadFileMutation = useMutation({
    mutationFn: async (file: File) => {
      const client = await getClient(credentialGetter);
      const formData = new FormData();
      formData.append("file", file);
      return client.post<
        FormData,
        {
          data: {
            s3_uri: string;
            presigned_url: string;
          };
        }
      >("/upload_file", formData, {
        headers: {
          "Content-Type": "multipart/form-data",
        },
      });
    },
    onSuccess: (response) => {
      onChange({
        s3uri: response.data.s3_uri,
        presignedUrl: response.data.presigned_url,
      });
    },
    onError: (error) => {
      setFile(null);
      toast({
        variant: "destructive",
        title: "Failed to upload file",
        description: `An error occurred while uploading the file: ${error.message}`,
      });
    },
  });

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const file = e.target.files[0] as File;
      if (file.size > FILE_SIZE_LIMIT_IN_BYTES) {
        showFileSizeError();
        return;
      }
      setFile(file);
      uploadFileMutation.mutate(file);
    }
  };

  function reset() {
    setFile(null);
    onChange(null);
  }

  const isManualUpload =
    typeof value === "object" && value !== null && file && "s3uri" in value;

  return (
    <Tabs
      className="h-36 w-full"
      defaultValue="upload"
      value={value === null ? undefined : isManualUpload ? "upload" : "fileURL"}
      onValueChange={(value) => {
        if (value === "upload") {
          onChange(null);
        } else {
          onChange("");
        }
      }}
    >
      <TabsList className="grid w-full grid-cols-2">
        <TabsTrigger value="upload">Upload</TabsTrigger>
        <TabsTrigger value="fileURL">File URL</TabsTrigger>
      </TabsList>
      <TabsContent value="upload">
        {isManualUpload && ( // redundant check for ts compiler
          <div className="flex h-full items-center gap-4 p-4">
            <a href={value.presignedUrl} className="underline">
              <div className="flex gap-2">
                <FileIcon className="size-6" />
                <span>{file.name}</span>
              </div>
            </a>
            <Button onClick={() => reset()} size="icon" variant="secondary">
              <Cross2Icon />
            </Button>
          </div>
        )}
        {value === null && (
          <Label
            htmlFor={inputId}
            className={cn(
              "flex w-full cursor-pointer items-center justify-center border border-dashed py-8 hover:border-slate-500",
            )}
            onDragOver={(event) => {
              event.preventDefault();
            }}
            onDrop={(event) => {
              event.preventDefault();
              event.stopPropagation();
              if (
                event.dataTransfer.files &&
                event.dataTransfer.files.length > 0
              ) {
                const file = event.dataTransfer.files[0] as File;
                if (file.size > FILE_SIZE_LIMIT_IN_BYTES) {
                  showFileSizeError();
                  return;
                }
                setFile(file);
                uploadFileMutation.mutate(file);
              }
            }}
          >
            <input
              id={inputId}
              type="file"
              onChange={handleFileChange}
              accept=".csv,.pdf"
              className="hidden"
            />
            <div className="flex max-w-full gap-2 px-2">
              {uploadFileMutation.isPending && (
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
              )}
              <span>
                {file
                  ? file.name
                  : "Drag and drop file here or click to select (Max 10MB)"}
              </span>
            </div>
          </Label>
        )}
      </TabsContent>
      <TabsContent value="fileURL">
        <div className="space-y-2">
          <Label>File URL</Label>
          {typeof value === "string" && (
            <Input
              value={value}
              onChange={(event) => onChange(event.target.value)}
            />
          )}
        </div>
      </TabsContent>
    </Tabs>
  );
}

export { FileUpload };
