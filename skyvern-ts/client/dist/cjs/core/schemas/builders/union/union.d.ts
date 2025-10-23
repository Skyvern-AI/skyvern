import { type ObjectLikeSchema } from "../object-like/index.js";
import type { Discriminant } from "./discriminant.js";
import type { inferParsedUnion, inferRawUnion, UnionSubtypes } from "./types.js";
export declare function union<D extends string | Discriminant<any, any>, U extends UnionSubtypes<any>>(discriminant: D, union: U): ObjectLikeSchema<inferRawUnion<D, U>, inferParsedUnion<D, U>>;
