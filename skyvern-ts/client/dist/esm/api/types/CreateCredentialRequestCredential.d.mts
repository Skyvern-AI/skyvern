import type * as Skyvern from "../index.mjs";
/**
 * The credential data to store
 */
export type CreateCredentialRequestCredential = Skyvern.NonEmptyPasswordCredential | Skyvern.NonEmptyCreditCardCredential;
