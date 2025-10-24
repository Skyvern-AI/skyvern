import { type HttpResponseResolver, passthrough } from "msw";

import { fromJson, toJson } from "../../src/core/json";

/**
 * Creates a request matcher that validates if the request JSON body exactly matches the expected object
 * @param expectedBody - The exact body object to match against
 * @param resolver - Response resolver to execute if body matches
 */
export function withJson(expectedBody: unknown, resolver: HttpResponseResolver): HttpResponseResolver {
    return async (args) => {
        const { request } = args;

        let clonedRequest: Request;
        let bodyText: string | undefined;
        let actualBody: unknown;
        try {
            clonedRequest = request.clone();
            bodyText = await clonedRequest.text();
            if (bodyText === "") {
                console.error("Request body is empty, expected a JSON object.");
                return passthrough();
            }
            actualBody = fromJson(bodyText);
        } catch (error) {
            console.error(`Error processing request body:\n\tError: ${error}\n\tBody: ${bodyText}`);
            return passthrough();
        }

        const mismatches = findMismatches(actualBody, expectedBody);
        if (Object.keys(mismatches).filter((key) => !key.startsWith("pagination.")).length > 0) {
            console.error("JSON body mismatch:", toJson(mismatches, undefined, 2));
            return passthrough();
        }

        return resolver(args);
    };
}

function findMismatches(actual: any, expected: any): Record<string, { actual: any; expected: any }> {
    const mismatches: Record<string, { actual: any; expected: any }> = {};

    if (typeof actual !== typeof expected) {
        if (areEquivalent(actual, expected)) {
            return {};
        }
        return { value: { actual, expected } };
    }

    if (typeof actual !== "object" || actual === null || expected === null) {
        if (actual !== expected) {
            if (areEquivalent(actual, expected)) {
                return {};
            }
            return { value: { actual, expected } };
        }
        return {};
    }

    if (Array.isArray(actual) && Array.isArray(expected)) {
        if (actual.length !== expected.length) {
            return { length: { actual: actual.length, expected: expected.length } };
        }

        const arrayMismatches: Record<string, { actual: any; expected: any }> = {};
        for (let i = 0; i < actual.length; i++) {
            const itemMismatches = findMismatches(actual[i], expected[i]);
            if (Object.keys(itemMismatches).length > 0) {
                for (const [mismatchKey, mismatchValue] of Object.entries(itemMismatches)) {
                    arrayMismatches[`[${i}]${mismatchKey === "value" ? "" : `.${mismatchKey}`}`] = mismatchValue;
                }
            }
        }
        return arrayMismatches;
    }

    const actualKeys = Object.keys(actual);
    const expectedKeys = Object.keys(expected);

    const allKeys = new Set([...actualKeys, ...expectedKeys]);

    for (const key of allKeys) {
        if (!expectedKeys.includes(key)) {
            if (actual[key] === undefined) {
                continue; // Skip undefined values in actual
            }
            mismatches[key] = { actual: actual[key], expected: undefined };
        } else if (!actualKeys.includes(key)) {
            if (expected[key] === undefined) {
                continue; // Skip undefined values in expected
            }
            mismatches[key] = { actual: undefined, expected: expected[key] };
        } else if (
            typeof actual[key] === "object" &&
            actual[key] !== null &&
            typeof expected[key] === "object" &&
            expected[key] !== null
        ) {
            const nestedMismatches = findMismatches(actual[key], expected[key]);
            if (Object.keys(nestedMismatches).length > 0) {
                for (const [nestedKey, nestedValue] of Object.entries(nestedMismatches)) {
                    mismatches[`${key}${nestedKey === "value" ? "" : `.${nestedKey}`}`] = nestedValue;
                }
            }
        } else if (actual[key] !== expected[key]) {
            if (areEquivalent(actual[key], expected[key])) {
                continue;
            }
            mismatches[key] = { actual: actual[key], expected: expected[key] };
        }
    }

    return mismatches;
}

function areEquivalent(actual: unknown, expected: unknown): boolean {
    if (actual === expected) {
        return true;
    }
    if (isEquivalentBigInt(actual, expected)) {
        return true;
    }
    if (isEquivalentDatetime(actual, expected)) {
        return true;
    }
    return false;
}

function isEquivalentBigInt(actual: unknown, expected: unknown) {
    if (typeof actual === "number") {
        actual = BigInt(actual);
    }
    if (typeof expected === "number") {
        expected = BigInt(expected);
    }
    if (typeof actual === "bigint" && typeof expected === "bigint") {
        return actual === expected;
    }
    return false;
}

function isEquivalentDatetime(str1: unknown, str2: unknown): boolean {
    if (typeof str1 !== "string" || typeof str2 !== "string") {
        return false;
    }
    const isoDatePattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/;
    if (!isoDatePattern.test(str1) || !isoDatePattern.test(str2)) {
        return false;
    }

    try {
        const date1 = new Date(str1).getTime();
        const date2 = new Date(str2).getTime();
        return date1 === date2;
    } catch {
        return false;
    }
}
