export interface ValidationError {
    loc: ValidationError.Loc.Item[];
    msg: string;
    type: string;
}
export declare namespace ValidationError {
    type Loc = Loc.Item[];
    namespace Loc {
        type Item = string | number;
    }
}
