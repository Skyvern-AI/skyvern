import { createRequestUrl } from "../../../src/core/fetcher/createRequestUrl";

describe("Test createRequestUrl", () => {
    it("should return the base URL when no query parameters are provided", () => {
        const baseUrl = "https://api.example.com";
        expect(createRequestUrl(baseUrl)).toBe(baseUrl);
    });

    it("should append simple query parameters", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { key: "value", another: "param" };
        expect(createRequestUrl(baseUrl, queryParams)).toBe("https://api.example.com?key=value&another=param");
    });

    it("should handle array query parameters", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { items: ["a", "b", "c"] };
        expect(createRequestUrl(baseUrl, queryParams)).toBe("https://api.example.com?items=a&items=b&items=c");
    });

    it("should handle object query parameters", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { filter: { name: "John", age: 30 } };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?filter%5Bname%5D=John&filter%5Bage%5D=30",
        );
    });

    it("should handle mixed types of query parameters", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = {
            simple: "value",
            array: ["x", "y"],
            object: { key: "value" },
        };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?simple=value&array=x&array=y&object%5Bkey%5D=value",
        );
    });

    it("should handle empty query parameters object", () => {
        const baseUrl = "https://api.example.com";
        expect(createRequestUrl(baseUrl, {})).toBe(baseUrl);
    });

    it("should encode special characters in query parameters", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { special: "a&b=c d" };
        expect(createRequestUrl(baseUrl, queryParams)).toBe("https://api.example.com?special=a%26b%3Dc%20d");
    });

    // Additional tests for edge cases and different value types
    it("should handle numeric values", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { count: 42, price: 19.99, active: 1, inactive: 0 };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?count=42&price=19.99&active=1&inactive=0",
        );
    });

    it("should handle boolean values", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { enabled: true, disabled: false };
        expect(createRequestUrl(baseUrl, queryParams)).toBe("https://api.example.com?enabled=true&disabled=false");
    });

    it("should handle null and undefined values", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = {
            valid: "value",
            nullValue: null,
            undefinedValue: undefined,
            emptyString: "",
        };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?valid=value&nullValue=&emptyString=",
        );
    });

    it("should handle deeply nested objects", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = {
            user: {
                profile: {
                    name: "John",
                    settings: { theme: "dark" },
                },
            },
        };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?user%5Bprofile%5D%5Bname%5D=John&user%5Bprofile%5D%5Bsettings%5D%5Btheme%5D=dark",
        );
    });

    it("should handle arrays of objects", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = {
            users: [
                { name: "John", age: 30 },
                { name: "Jane", age: 25 },
            ],
        };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?users%5Bname%5D=John&users%5Bage%5D=30&users%5Bname%5D=Jane&users%5Bage%5D=25",
        );
    });

    it("should handle mixed arrays", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = {
            mixed: ["string", 42, true, { key: "value" }],
        };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?mixed=string&mixed=42&mixed=true&mixed%5Bkey%5D=value",
        );
    });

    it("should handle empty arrays", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { emptyArray: [] };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(baseUrl);
    });

    it("should handle empty objects", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { emptyObject: {} };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(baseUrl);
    });

    it("should handle special characters in keys", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = { "key with spaces": "value", "key[with]brackets": "value" };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?key%20with%20spaces=value&key%5Bwith%5Dbrackets=value",
        );
    });

    it("should handle URL with existing query parameters", () => {
        const baseUrl = "https://api.example.com?existing=param";
        const queryParams = { new: "value" };
        expect(createRequestUrl(baseUrl, queryParams)).toBe("https://api.example.com?existing=param?new=value");
    });

    it("should handle complex nested structures", () => {
        const baseUrl = "https://api.example.com";
        const queryParams = {
            filters: {
                status: ["active", "pending"],
                category: {
                    type: "electronics",
                    subcategories: ["phones", "laptops"],
                },
            },
            sort: { field: "name", direction: "asc" },
        };
        expect(createRequestUrl(baseUrl, queryParams)).toBe(
            "https://api.example.com?filters%5Bstatus%5D=active&filters%5Bstatus%5D=pending&filters%5Bcategory%5D%5Btype%5D=electronics&filters%5Bcategory%5D%5Bsubcategories%5D=phones&filters%5Bcategory%5D%5Bsubcategories%5D=laptops&sort%5Bfield%5D=name&sort%5Bdirection%5D=asc",
        );
    });
});
