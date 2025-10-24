import { toJson } from "../json.js";

export declare namespace GetRequestBody {
    interface Args {
        body: unknown;
        type: "json" | "file" | "bytes" | "other";
    }
}

export async function getRequestBody({ body, type }: GetRequestBody.Args): Promise<BodyInit | undefined> {
    if (type.includes("json")) {
        return toJson(body);
    } else {
        return body as BodyInit;
    }
}
