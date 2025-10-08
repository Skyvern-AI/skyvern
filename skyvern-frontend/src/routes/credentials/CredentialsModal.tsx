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
import { PasswordCredentialContent } from "./PasswordCredentialContent";
import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { CreditCardCredentialContent } from "./CreditCardCredentialContent";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CreateCredentialRequest } from "@/api/types";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { AxiosError } from "axios";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useCredentialsQuery } from "@/routes/workflows/hooks/useCredentialsQuery";

const PASSWORD_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  username: "",
  password: "",
  totp: "",
  totp_type: "none" as "none" | "authenticator" | "email" | "text",
};

const CREDIT_CARD_CREDENTIAL_INITIAL_VALUES = {
  name: "",
  cardNumber: "",
  cardExpirationDate: "",
  cardCode: "",
  cardBrand: "",
  cardHolderName: "",
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
};

function CredentialsModal({ onCredentialCreated }: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { isOpen, type, setIsOpen } = useCredentialModalState();
  const { data: credentials } = useCredentialsQuery();
  const [passwordCredentialValues, setPasswordCredentialValues] = useState(
    PASSWORD_CREDENTIAL_INITIAL_VALUES,
  );
  const [creditCardCredentialValues, setCreditCardCredentialValues] = useState(
    CREDIT_CARD_CREDENTIAL_INITIAL_VALUES,
  );

  // Set default name when modal opens
  useEffect(() => {
    if (isOpen && credentials) {
      const existingNames = credentials.map((cred) => cred.name);
      const defaultName = generateDefaultCredentialName(existingNames);

      setPasswordCredentialValues((prev) => ({
        ...prev,
        name: defaultName,
      }));
      setCreditCardCredentialValues((prev) => ({
        ...prev,
        name: defaultName,
      }));
    }
  }, [isOpen, credentials]);

  function reset() {
    setPasswordCredentialValues(PASSWORD_CREDENTIAL_INITIAL_VALUES);
    setCreditCardCredentialValues(CREDIT_CARD_CREDENTIAL_INITIAL_VALUES);
  }

  const createCredentialMutation = useMutation({
    mutationFn: async (request: CreateCredentialRequest) => {
      const client = await getClient(credentialGetter);
      const response = await client.post("/credentials", request);
      return response.data;
    },
    onSuccess: (data) => {
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

  const handleSave = () => {
    const name =
      type === CredentialModalTypes.PASSWORD
        ? passwordCredentialValues.name.trim()
        : creditCardCredentialValues.name.trim();
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

      if (username === "" || password === "") {
        toast({
          title: "Error",
          description: "Username and password are required",
          variant: "destructive",
        });
        return;
      }
      createCredentialMutation.mutate({
        name,
        credential_type: "password",
        credential: {
          username,
          password,
          totp: totp === "" ? null : totp,
          totp_type: passwordCredentialValues.totp_type,
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
      createCredentialMutation.mutate({
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
    }
  };

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
          <DialogTitle className="font-bold">Add Credential</DialogTitle>
        </DialogHeader>
        {type === CredentialModalTypes.PASSWORD ? (
          <PasswordCredentialContent
            values={passwordCredentialValues}
            onChange={setPasswordCredentialValues}
          />
        ) : (
          <CreditCardCredentialContent
            values={creditCardCredentialValues}
            onChange={setCreditCardCredentialValues}
          />
        )}
        <DialogFooter>
          <Button
            onClick={handleSave}
            disabled={createCredentialMutation.isPending}
          >
            {createCredentialMutation.isPending ? (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            ) : null}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { CredentialsModal };
