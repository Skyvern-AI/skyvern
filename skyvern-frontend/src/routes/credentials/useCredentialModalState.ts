import { useSearchParams } from "react-router-dom";

const modalParam = "modal";
const typeParam = "type";

export const CredentialModalTypes = {
  PASSWORD: "password",
  CREDIT_CARD: "credit-card",
  SECRET: "secret",
} as const;

export type CredentialModalType =
  (typeof CredentialModalTypes)[keyof typeof CredentialModalTypes];

type ReturnType = {
  isOpen: boolean;
  type: CredentialModalType;
  setIsOpen: (isOpen: boolean) => void;
  setType: (type: CredentialModalType) => void;
};

function getCredentialModalType(type: string): CredentialModalType {
  if (
    Object.values(CredentialModalTypes).includes(type as CredentialModalType)
  ) {
    return type as CredentialModalType;
  }
  return CredentialModalTypes.PASSWORD;
}

function useCredentialModalState(): ReturnType {
  const [searchParams, setSearchParams] = useSearchParams();

  const modal = searchParams.get(modalParam);
  const isOpen = modal === "true";
  const type = getCredentialModalType(searchParams.get(typeParam) ?? "");

  const setIsOpen = (isOpen: boolean) => {
    setSearchParams((prev) => {
      prev.set(modalParam, isOpen.toString());
      return prev;
    });
  };

  const setType = (type: CredentialModalType) => {
    setSearchParams((prev) => {
      prev.set(typeParam, type);
      return prev;
    });
  };

  return {
    isOpen,
    type,
    setIsOpen,
    setType,
  };
}

export { useCredentialModalState };
