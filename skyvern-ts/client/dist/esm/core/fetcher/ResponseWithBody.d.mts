export type ResponseWithBody = Response & {
    body: ReadableStream<Uint8Array>;
};
export declare function isResponseWithBody(response: Response): response is ResponseWithBody;
