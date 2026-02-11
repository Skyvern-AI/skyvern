import { setupServer } from "msw/node";

import { fromJson, toJson } from "../../src/core/json";
import { MockServer } from "./MockServer";
import { randomBaseUrl } from "./randomBaseUrl";

const mswServer = setupServer();
interface MockServerOptions {
    baseUrl?: string;
}

async function formatHttpRequest(request: Request, id?: string): Promise<string> {
    try {
        const clone = request.clone();
        const headers = [...clone.headers.entries()].map(([k, v]) => `${k}: ${v}`).join("\n");

        let body = "";
        try {
            const contentType = clone.headers.get("content-type");
            if (contentType?.includes("application/json")) {
                body = toJson(fromJson(await clone.text()), undefined, 2);
            } else if (clone.body) {
                body = await clone.text();
            }
        } catch (_e) {
            body = "(unable to parse body)";
        }

        const title = id ? `### Request ${id} ###\n` : "";
        const firstLine = `${title}${request.method} ${request.url.toString()} HTTP/1.1`;

        return `\n${firstLine}\n${headers}\n\n${body || "(no body)"}\n`;
    } catch (e) {
        return `Error formatting request: ${e}`;
    }
}

async function formatHttpResponse(response: Response, id?: string): Promise<string> {
    try {
        const clone = response.clone();
        const headers = [...clone.headers.entries()].map(([k, v]) => `${k}: ${v}`).join("\n");

        let body = "";
        try {
            const contentType = clone.headers.get("content-type");
            if (contentType?.includes("application/json")) {
                body = toJson(fromJson(await clone.text()), undefined, 2);
            } else if (clone.body) {
                body = await clone.text();
            }
        } catch (_e) {
            body = "(unable to parse body)";
        }

        const title = id ? `### Response for ${id} ###\n` : "";
        const firstLine = `${title}HTTP/1.1 ${response.status} ${response.statusText}`;

        return `\n${firstLine}\n${headers}\n\n${body || "(no body)"}\n`;
    } catch (e) {
        return `Error formatting response: ${e}`;
    }
}

class MockServerPool {
    private servers: MockServer[] = [];

    public createServer(options?: Partial<MockServerOptions>): MockServer {
        const baseUrl = options?.baseUrl || randomBaseUrl();
        const server = new MockServer({ baseUrl, server: mswServer });
        this.servers.push(server);
        return server;
    }

    public getServers(): MockServer[] {
        return [...this.servers];
    }

    public listen(): void {
        const onUnhandledRequest = process.env.LOG_LEVEL === "debug" ? "warn" : "bypass";
        mswServer.listen({ onUnhandledRequest });

        if (process.env.LOG_LEVEL === "debug") {
            mswServer.events.on("request:start", async ({ request, requestId }) => {
                const formattedRequest = await formatHttpRequest(request, requestId);
                console.debug(`request:start\n${formattedRequest}`);
            });

            mswServer.events.on("request:unhandled", async ({ request, requestId }) => {
                const formattedRequest = await formatHttpRequest(request, requestId);
                console.debug(`request:unhandled\n${formattedRequest}`);
            });

            mswServer.events.on("response:mocked", async ({ request, response, requestId }) => {
                const formattedResponse = await formatHttpResponse(response, requestId);
                console.debug(`response:mocked\n${formattedResponse}`);
            });
        }
    }

    public close(): void {
        this.servers = [];
        mswServer.close();
    }
}

export const mockServerPool = new MockServerPool();
