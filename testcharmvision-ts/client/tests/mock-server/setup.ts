import { afterAll, beforeAll } from "vitest";

import { mockServerPool } from "./MockServerPool";

beforeAll(() => {
    mockServerPool.listen();
});
afterAll(() => {
    mockServerPool.close();
});
