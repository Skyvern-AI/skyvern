import React, { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { PlusIcon, TrashIcon } from "@radix-ui/react-icons";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";
import { 
  TaskCredentialConfig, 
  TaskCredentialType,
  SkyvernCredentialConfig,
  BitwardenCredentialConfig,
  OnePasswordCredentialConfig 
} from "@/api/types";

type Props = {
  credentials: TaskCredentialConfig[];
  onChange: (credentials: TaskCredentialConfig[]) => void;
};

function TaskCredentialSelector({ credentials, onChange }: Props) {
  const { data: availableCredentials, isFetching } = useCredentialsQuery();
  const [showAddForm, setShowAddForm] = useState(false);
  const [newCredential, setNewCredential] = useState<Partial<TaskCredentialConfig>>({
    credential_type: "skyvern",
    key: "",
    description: "",
  });

  const addCredential = () => {
    if (!newCredential.key || !newCredential.credential_type) return;

    let credential: TaskCredentialConfig;
    
    if (newCredential.credential_type === "skyvern") {
      credential = {
        credential_type: "skyvern",
        key: newCredential.key,
        description: newCredential.description,
        credential_id: (newCredential as SkyvernCredentialConfig).credential_id || "",
      };
    } else if (newCredential.credential_type === "bitwarden") {
      credential = {
        credential_type: "bitwarden",
        key: newCredential.key,
        description: newCredential.description,
        bitwarden_client_id_aws_secret_key: (newCredential as BitwardenCredentialConfig).bitwarden_client_id_aws_secret_key || "",
        bitwarden_client_secret_aws_secret_key: (newCredential as BitwardenCredentialConfig).bitwarden_client_secret_aws_secret_key || "",
        bitwarden_master_password_aws_secret_key: (newCredential as BitwardenCredentialConfig).bitwarden_master_password_aws_secret_key || "",
        bitwarden_collection_id: (newCredential as BitwardenCredentialConfig).bitwarden_collection_id,
        bitwarden_item_id: (newCredential as BitwardenCredentialConfig).bitwarden_item_id,
        url_parameter_key: (newCredential as BitwardenCredentialConfig).url_parameter_key,
      };
    } else {
      credential = {
        credential_type: "onepassword",
        key: newCredential.key,
        description: newCredential.description,
        vault_id: (newCredential as OnePasswordCredentialConfig).vault_id || "",
        item_id: (newCredential as OnePasswordCredentialConfig).item_id || "",
      };
    }

    onChange([...credentials, credential]);
    setNewCredential({
      credential_type: "skyvern",
      key: "",
      description: "",
    });
    setShowAddForm(false);
  };

  const removeCredential = (index: number) => {
    const updated = credentials.filter((_, i) => i !== index);
    onChange(updated);
  };

  const updateCredential = (index: number, field: string, value: string) => {
    const updated = [...credentials];
    (updated[index] as any)[field] = value;
    onChange(updated);
  };

  if (isFetching) {
    return <Skeleton className="h-20 w-full" />;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Label className="text-sm font-medium">Task Credentials</Label>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setShowAddForm(true)}
          className="flex items-center gap-2"
        >
          <PlusIcon className="h-4 w-4" />
          Add Credential
        </Button>
      </div>

      {/* Existing credentials list */}
      {credentials.length > 0 && (
        <div className="space-y-3">
          {credentials.map((credential, index) => (
            <div
              key={index}
              className="flex items-center gap-3 p-3 border rounded-md bg-slate-50"
            >
              <div className="flex-1 space-y-2">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{credential.key}</span>
                  <span className="text-xs px-2 py-1 bg-blue-100 text-blue-800 rounded">
                    {credential.credential_type}
                  </span>
                </div>
                {credential.description && (
                  <p className="text-xs text-gray-600">{credential.description}</p>
                )}
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => removeCredential(index)}
                className="text-red-600 hover:text-red-800"
              >
                <TrashIcon className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </div>
      )}

      {/* Add credential form */}
      {showAddForm && (
        <div className="space-y-4 p-4 border rounded-md bg-slate-50">
          <div className="flex items-center justify-between">
            <h4 className="font-medium">Add New Credential</h4>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setShowAddForm(false)}
            >
              Cancel
            </Button>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label className="text-xs">Credential Type</Label>
              <Select
                value={newCredential.credential_type}
                onValueChange={(value: TaskCredentialType) =>
                  setNewCredential({ ...newCredential, credential_type: value })
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="skyvern">Skyvern Credential</SelectItem>
                  <SelectItem value="bitwarden">Bitwarden</SelectItem>
                  <SelectItem value="onepassword">1Password</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label className="text-xs">Key</Label>
              <Input
                value={newCredential.key}
                onChange={(e) =>
                  setNewCredential({ ...newCredential, key: e.target.value })
                }
                placeholder="e.g., login_credentials"
              />
            </div>
          </div>

          <div className="space-y-2">
            <Label className="text-xs">Description (Optional)</Label>
            <Input
              value={newCredential.description || ""}
              onChange={(e) =>
                setNewCredential({ ...newCredential, description: e.target.value })
              }
              placeholder="Description of what this credential is for"
            />
          </div>

          {/* Skyvern credential specific fields */}
          {newCredential.credential_type === "skyvern" && (
            <div className="space-y-2">
              <Label className="text-xs">Skyvern Credential</Label>
              <Select
                value={(newCredential as SkyvernCredentialConfig).credential_id || ""}
                onValueChange={(value) =>
                  setNewCredential({ 
                    ...newCredential, 
                    credential_id: value 
                  } as SkyvernCredentialConfig)
                }
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select a credential" />
                </SelectTrigger>
                <SelectContent>
                  {availableCredentials?.map((credential) => (
                    <SelectItem key={credential.credential_id} value={credential.credential_id}>
                      {credential.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          {/* Bitwarden credential specific fields */}
          {newCredential.credential_type === "bitwarden" && (
            <div className="space-y-3">
              <div className="grid grid-cols-1 gap-3">
                <div className="space-y-2">
                  <Label className="text-xs">Client ID AWS Secret Key</Label>
                  <Input
                    value={(newCredential as BitwardenCredentialConfig).bitwarden_client_id_aws_secret_key || ""}
                    onChange={(e) =>
                      setNewCredential({ 
                        ...newCredential, 
                        bitwarden_client_id_aws_secret_key: e.target.value 
                      } as BitwardenCredentialConfig)
                    }
                    placeholder="AWS secret key for Bitwarden client ID"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-xs">Client Secret AWS Secret Key</Label>
                  <Input
                    value={(newCredential as BitwardenCredentialConfig).bitwarden_client_secret_aws_secret_key || ""}
                    onChange={(e) =>
                      setNewCredential({ 
                        ...newCredential, 
                        bitwarden_client_secret_aws_secret_key: e.target.value 
                      } as BitwardenCredentialConfig)
                    }
                    placeholder="AWS secret key for Bitwarden client secret"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="text-xs">Master Password AWS Secret Key</Label>
                  <Input
                    value={(newCredential as BitwardenCredentialConfig).bitwarden_master_password_aws_secret_key || ""}
                    onChange={(e) =>
                      setNewCredential({ 
                        ...newCredential, 
                        bitwarden_master_password_aws_secret_key: e.target.value 
                      } as BitwardenCredentialConfig)
                    }
                    placeholder="AWS secret key for Bitwarden master password"
                  />
                </div>
              </div>
            </div>
          )}

          {/* 1Password credential specific fields */}
          {newCredential.credential_type === "onepassword" && (
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label className="text-xs">Vault ID</Label>
                <Input
                  value={(newCredential as OnePasswordCredentialConfig).vault_id || ""}
                  onChange={(e) =>
                    setNewCredential({ 
                      ...newCredential, 
                      vault_id: e.target.value 
                    } as OnePasswordCredentialConfig)
                  }
                  placeholder="1Password vault ID"
                />
              </div>
              <div className="space-y-2">
                <Label className="text-xs">Item ID</Label>
                <Input
                  value={(newCredential as OnePasswordCredentialConfig).item_id || ""}
                  onChange={(e) =>
                    setNewCredential({ 
                      ...newCredential, 
                      item_id: e.target.value 
                    } as OnePasswordCredentialConfig)
                  }
                  placeholder="1Password item ID"
                />
              </div>
            </div>
          )}

          <Button
            type="button"
            onClick={addCredential}
            disabled={!newCredential.key || !newCredential.credential_type}
            className="w-full"
          >
            Add Credential
          </Button>
        </div>
      )}

      {credentials.length === 0 && !showAddForm && (
        <div className="text-center py-8 text-gray-500">
          <p className="text-sm">No credentials configured</p>
          <p className="text-xs">Click "Add Credential" to get started</p>
        </div>
      )}
    </div>
  );
}

export { TaskCredentialSelector }; 