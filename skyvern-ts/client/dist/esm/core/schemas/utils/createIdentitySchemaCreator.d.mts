import type { MaybeValid, Schema, SchemaOptions, SchemaType } from "../Schema.mjs";
export declare function createIdentitySchemaCreator<T>(schemaType: SchemaType, validate: (value: unknown, opts?: SchemaOptions) => MaybeValid<T>): () => Schema<T, T>;
