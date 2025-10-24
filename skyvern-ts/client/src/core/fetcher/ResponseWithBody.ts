export type ResponseWithBody = Response & {
    body: ReadableStream<Uint8Array>;
};

export function isResponseWithBody(response: Response): response is ResponseWithBody {
    return (response as ResponseWithBody).body != null;
}
