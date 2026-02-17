import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogHeader,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  useCredentialModalState,
  CredentialModalTypes,
} from "./useCredentialModalState";
import type { CredentialModalType } from "./useCredentialModalState";
import { PasswordCredentialContent } from "./PasswordCredentialContent";
import { SecretCredentialContent } from "./SecretCredentialContent";
import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { CreditCardCredentialContent } from "./CreditCardCredentialContent";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CreateCredentialRequest,
  CredentialApiResponse,
  isPasswordCredential,
  isCreditCardCredential,
  isSecretCredential,
} from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { AxiosError } from "axios";
import { InfoCircledIcon, ReloadIcon } from "@radix-ui/react-icons";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";

const PASSWORD_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  username: "",
  password: "",
  totp: "",
  totp_type: "none" as "none" | "authenticator" | "email" | "text",
  totp_identifier: "",
};

const CREDIT_CARD_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  cardNumber: "",
  cardExpirationDate: "",
  cardCode: "",
  cardBrand: "",
  cardHolderName: "",
};

const SECRET_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  secretLabel: "",
  secretValue: "",
};

// Function to generate a unique credential name
function generateDefaultCredentialName(existingNames: string[]): string {
  const baseName = "credentials";

  // Check if "credentials" is available
  if (!existingNames.includes(baseName)) {
    return baseName;
  }

  // Find the next available number
  let counter = 1;
  while (existingNames.includes(`${baseName}_${counter}`)) {
    counter++;
  }

  return `${baseName}_${counter}`;
}

type Props = {
  onCredentialCreated?: (id: string) => void;
  /** Optional controlled mode: pass isOpen and onOpenChange to control modal state locally */
  isOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** When provided, the modal opens in edit mode and pre-fills available fields */
  editingCredential?: CredentialApiResponse;
  /** Override the modal type (used in edit mode to set the correct form) */
  overrideType?: CredentialModalType;
};

function CredentialsModal({
  onCredentialCreated,
  isOpen: controlledIsOpen,
  onOpenChange: controlledOnOpenChange,
  editingCredential,
  overrideType,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const {
    isOpen: urlIsOpen,
    type: urlType,
    setIsOpen: setUrlIsOpen,
  } = useCredentialModalState();

  const isEditMode = !!editingCredential;

  // Use controlled props if provided, otherwise fall back to URL-based state
  const isOpen = controlledIsOpen ?? urlIsOpen;
  const setIsOpen = controlledOnOpenChange ?? setUrlIsOpen;
  const type = overrideType ?? urlType;
  const { data: credentials } = useCredentialsQuery({
    page_size: 100,
  });
  const [passwordCredentialValues, setPasswordCredentialValues] = useState(
    PASSWORD_CREDENTIAL_INITIAL_VALUES,
  );
  const [creditCardCredentialValues, setCreditCardCredentialValues] = useState(
    CREDIT_CARD_CREDENTIAL_INITIAL_VALUES,
  );
  const [secretCredentialValues, setSecretCredentialValues] = useState(
    SECRET_CREDENTIAL_INITIAL_VALUES,
  );

  // Set default name when modal opens, or pre-populate fields in edit mode
  useEffect(() => {
    if (!isOpen) return;

    if (isEditMode) {
      reset();
      const cred = editingCredential.credential;
      if (isPasswordCredential(cred)) {
        setPasswordCredentialValues({
          name: editingCredential.name,
          username: cred.username,
          password: "",
          totp: "",
          totp_type: cred.totp_type,
          totp_identifier: cred.totp_identifier ?? "",
        });
      } else if (isCreditCardCredential(cred)) {
        setCreditCardCredentialValues({
          name: editingCredential.name,
          cardNumber: "",
          cardExpirationDate: "",
          cardCode: "",
          cardBrand: cred.brand,
          cardHolderName: "",
        });
      } else if (isSecretCredential(cred)) {
        setSecretCredentialValues({
          name: editingCredential.name,
          secretLabel: cred.secret_label ?? "",
          secretValue: "",
        });
      }
      return;
    }

    if (credentials) {
      const existingNames = credentials.map((c) => c.name);
      const defaultName = generateDefaultCredentialName(existingNames);

      setPasswordCredentialValues((prev) => ({
        ...prev,
        name: defaultName,
      }));
      setCreditCardCredentialValues((prev) => ({
        ...prev,
        name: defaultName,
      }));
      setSecretCredentialValues((prev) => ({
        ...prev,
        name: defaultName,
      }));
    }
  }, [isOpen, credentials, isEditMode, editingCredential]);

  function reset() {
    setPasswordCredentialValues(PASSWORD_CREDENTIAL_INITIAL_VALUES);
    setCreditCardCredentialValues(CREDIT_CARD_CREDENTIAL_INITIAL_VALUES);
    setSecretCredentialValues(SECRET_CREDENTIAL_INITIAL_VALUES);
  }

  const createCredentialMutation = useMutation({
    mutationFn: async (request: CreateCredentialRequest) => {
      const client = await getClient(credentialGetter);
      const response = await client.post("/credentials", request);
      return response.data;
    },
    onSuccess: (data) => {
      reset();
      setIsOpen(false);
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      toast({
        title: "Credential created",
        description: "Your credential has been created successfully",
        variant: "success",
      });
      onCredentialCreated?.(data.credential_id);
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Error",
        description: detail ? detail : error.message,
        variant: "destructive",
      });
    },
  });

  const updateCredentialMutation = useMutation({
    mutationFn: async (request: CreateCredentialRequest) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.post(
        `/credentials/${editingCredential?.credential_id}/update`,
        request,
      );
      return response.data;
    },
    onSuccess: () => {
      reset();
      setIsOpen(false);
      queryClient.invalidateQueries({
        queryKey: ["credentials"],
      });
      toast({
        title: "Credential updated",
        description: "Your credential has been updated successfully",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        title: "Error",
        description: detail ? detail : error.message,
        variant: "destructive",
      });
    },
  });

  const activeMutation = isEditMode
    ? updateCredentialMutation
    : createCredentialMutation;

  const handleSave = () => {
    const name =
      type === CredentialModalTypes.PASSWORD
        ? passwordCredentialValues.name.trim()
        : type === CredentialModalTypes.CREDIT_CARD
          ? creditCardCredentialValues.name.trim()
          : secretCredentialValues.name.trim();
    if (name === "") {
      toast({
        title: "Error",
        description: "Name is required",
        variant: "destructive",
      });
      return;
    }

    if (type === CredentialModalTypes.PASSWORD) {
      const username = passwordCredentialValues.username.trim();
      const password = passwordCredentialValues.password.trim();
      const totp = passwordCredentialValues.totp.trim();
      const totpIdentifier = passwordCredentialValues.totp_identifier.trim();

      if (username === "" || password === "") {
        toast({
          title: "Error",
          description: "Username and password are required",
          variant: "destructive",
        });
        return;
      }
      activeMutation.mutate({
        name,
        credential_type: "password",
        credential: {
          username,
          password,
          totp: totp === "" ? null : totp,
          totp_type: passwordCredentialValues.totp_type,
          totp_identifier: totpIdentifier === "" ? null : totpIdentifier,
        },
      });
    } else if (type === CredentialModalTypes.CREDIT_CARD) {
      const cardNumber = creditCardCredentialValues.cardNumber.trim();
      const cardCode = creditCardCredentialValues.cardCode.trim();
      const cardExpirationDate =
        creditCardCredentialValues.cardExpirationDate.trim();
      const cardBrand = creditCardCredentialValues.cardBrand.trim();
      const cardHolderName = creditCardCredentialValues.cardHolderName.trim();

      if (
        cardNumber === "" ||
        cardCode === "" ||
        cardExpirationDate === "" ||
        cardBrand === "" ||
        cardHolderName === ""
      ) {
        toast({
          title: "Error",
          description: "All credit card fields are required",
          variant: "destructive",
        });
        return;
      }

      const cardExpirationDateParts = cardExpirationDate.split("/");
      if (cardExpirationDateParts.length !== 2) {
        toast({
          title: "Error",
          description: "Invalid card expiration date",
          variant: "destructive",
        });
        return;
      }
      const cardExpirationMonth = cardExpirationDateParts[0];
      const cardExpirationYear = cardExpirationDateParts[1];
      if (!cardExpirationMonth || !cardExpirationYear) {
        toast({
          title: "Error",
          description: "Invalid card expiration date",
          variant: "destructive",
        });
        return;
      }
      // remove all spaces from the card number
      const number = creditCardCredentialValues.cardNumber.replace(/\s/g, "");
      activeMutation.mutate({
        name,
        credential_type: "credit_card",
        credential: {
          card_number: number,
          card_cvv: cardCode,
          card_exp_month: cardExpirationMonth,
          card_exp_year: cardExpirationYear,
          card_brand: cardBrand,
          card_holder_name: cardHolderName,
        },
      });
    } else if (type === CredentialModalTypes.SECRET) {
      const secretValue = secretCredentialValues.secretValue.trim();
      const secretLabel = secretCredentialValues.secretLabel.trim();

      if (secretValue === "") {
        toast({
          title: "Error",
          description: "Secret value is required",
          variant: "destructive",
        });
        return;
      }

      activeMutation.mutate({
        name,
        credential_type: "secret",
        credential: {
          secret_value: secretValue,
          secret_label: secretLabel === "" ? null : secretLabel,
        },
      });
    }
  };

  const credentialContent = (() => {
    if (type === CredentialModalTypes.PASSWORD) {
      return (
        <PasswordCredentialContent
          values={passwordCredentialValues}
          onChange={setPasswordCredentialValues}
        />
      );
    }
    if (type === CredentialModalTypes.CREDIT_CARD) {
      return (
        <CreditCardCredentialContent
          values={creditCardCredentialValues}
          onChange={setCreditCardCredentialValues}
        />
      );
    }
    return (
      <SecretCredentialContent
        values={secretCredentialValues}
        onChange={setSecretCredentialValues}
      />
    );
  })();

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) {
          reset();
        }
        setIsOpen(open);
      }}
    >
      <DialogContent className="w-[700px] max-w-[700px]">
        <DialogHeader>
          <DialogTitle className="font-bold">
            {isEditMode ? "Edit Credential" : "Add Credential"}
          </DialogTitle>
        </DialogHeader>
        {isEditMode && (
          <Alert>
            <InfoCircledIcon className="size-4" />
            <AlertDescription>
              For security, saved passwords and secrets are never retrieved.
              Please re-enter all fields to update this credential.
            </AlertDescription>
          </Alert>
        )}
        {credentialContent}
        <DialogFooter>
          <Button onClick={handleSave} disabled={activeMutation.isPending}>
            {activeMutation.isPending ? (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            ) : null}
            {isEditMode ? "Update" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { CredentialsModal };
