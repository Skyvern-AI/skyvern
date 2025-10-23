import type * as Skyvern from "../index.js";
/**
 * The credential data to store
 */
export type CreateCredentialRequestCredential = Skyvern.NonEmptyPasswordCredential | Skyvern.NonEmptyCreditCardCredential;
