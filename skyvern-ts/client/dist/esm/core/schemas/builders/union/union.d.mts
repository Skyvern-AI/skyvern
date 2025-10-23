import { type ObjectLikeSchema } from "../object-like/index.mjs";
import type { Discriminant } from "./discriminant.mjs";
import type { inferParsedUnion, inferRawUnion, UnionSubtypes } from "./types.mjs";
export declare function union<D extends string | Discriminant<any, any>, U extends UnionSubtypes<any>>(discriminant: D, union: U): ObjectLikeSchema<inferRawUnion<D, U>, inferParsedUnion<D, U>>;
