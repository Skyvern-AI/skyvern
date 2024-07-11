import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useMutation } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { toast } from "./ui/use-toast";

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
  const [fileUrl, setFileUrl] = useState<string>("");
  const [highlight, setHighlight] = useState(false);
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

  if (value === null) {
    return (
      <div className="flex gap-4">
        <div className="w-1/2">
          <Label
            htmlFor={inputId}
            className={cn(
              "flex w-full cursor-pointer border border-dashed items-center justify-center py-8",
              {
                "border-slate-500": highlight,
              },
            )}
            onDragEnter={(event) => {
              event.preventDefault();
              event.stopPropagation();
              setHighlight(true);
            }}
            onDragOver={(event) => {
              event.preventDefault();
              event.stopPropagation();
              setHighlight(true);
            }}
            onDragLeave={(event) => {
              event.preventDefault();
              event.stopPropagation();
              setHighlight(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              event.stopPropagation();
              setHighlight(false);
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
              accept=".csv"
              className="hidden"
            />
            <div className="max-w-full truncate flex gap-2">
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
        </div>
        <div className="flex flex-col items-center justify-center before:flex before:content-[''] before:bg-slate-600">
          OR
        </div>
        <div className="w-1/2">
          <Label>File URL</Label>
          <div className="flex gap-2">
            <Input
              value={fileUrl}
              onChange={(e) => setFileUrl(e.target.value)}
            />
            <Button
              onClick={() => {
                onChange(fileUrl);
              }}
            >
              Save
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (typeof value === "string") {
    return (
      <div className="flex gap-4 items-center">
        <span>{value}</span>
        <Button onClick={() => reset()}>Change</Button>
      </div>
    );
  }

  if (typeof value === "object" && file && "s3uri" in value) {
    return (
      <div className="flex gap-4 items-center">
        <a href={value.presignedUrl} className="underline">
          <span>{file.name}</span>
        </a>
        <Button onClick={() => reset()}>Change</Button>
      </div>
    );
  }
}

export { FileUpload };
