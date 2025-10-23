import type { inferParsedObject, inferRawObject, ObjectSchema } from "../object/index.js";
import type { Discriminant } from "./discriminant.js";
export type UnionSubtypes<DiscriminantValues extends string | number | symbol> = {
    [K in DiscriminantValues]: ObjectSchema<any, any>;
};
export type inferRawUnion<D extends string | Discriminant<any, any>, U extends UnionSubtypes<keyof U>> = {
    [K in keyof U]: Record<inferRawDiscriminant<D>, K> & inferRawObject<U[K]>;
}[keyof U];
export type inferParsedUnion<D extends string | Discriminant<any, any>, U extends UnionSubtypes<keyof U>> = {
    [K in keyof U]: Record<inferParsedDiscriminant<D>, K> & inferParsedObject<U[K]>;
}[keyof U];
export type inferRawDiscriminant<D extends string | Discriminant<any, any>> = D extends string ? D : D extends Discriminant<infer Raw, any> ? Raw : never;
export type inferParsedDiscriminant<D extends string | Discriminant<any, any>> = D extends string ? D : D extends Discriminant<any, infer Parsed> ? Parsed : never;
