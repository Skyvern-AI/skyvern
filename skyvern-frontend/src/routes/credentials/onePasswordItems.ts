import type { OnePasswordItemApiResponse } from "@/api/types";

type OnePasswordCredentialDataType = "password" | "secret" | "creditCard";

// 1Password reports categories as free-form strings that vary by item template
// (LOGIN, PASSWORD, CREDIT_CARD, ...), so match on substrings rather than an enum.
function isOnePasswordItemOfType(
  item: OnePasswordItemApiResponse,
  dataType: OnePasswordCredentialDataType,
): boolean {
  const category = item.category.toLowerCase();

  if (dataType === "password") {
    return category.includes("login") || category.includes("password");
  }

  if (dataType === "creditCard") {
    return category.includes("card");
  }

  return true;
}

export { isOnePasswordItemOfType };
export type { OnePasswordCredentialDataType };
