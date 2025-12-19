import { encodeAsFormParameter } from "../../../src/core/form-data-utils/encodeAsFormParameter";

describe("encodeAsFormParameter", () => {
    describe("Basic functionality", () => {
        it("should return empty object for null/undefined", () => {
            expect(encodeAsFormParameter(null)).toEqual({});
            expect(encodeAsFormParameter(undefined)).toEqual({});
        });

        it("should return empty object for primitive values", () => {
            expect(encodeAsFormParameter("hello")).toEqual({});
            expect(encodeAsFormParameter(42)).toEqual({});
            expect(encodeAsFormParameter(true)).toEqual({});
        });

        it("should handle simple key-value pairs", () => {
            const obj = { name: "John", age: 30 };
            expect(encodeAsFormParameter(obj)).toEqual({
                name: "John",
                age: "30",
            });
        });

        it("should handle empty objects", () => {
            expect(encodeAsFormParameter({})).toEqual({});
        });
    });

    describe("Array handling", () => {
        it("should handle arrays with indices format (default)", () => {
            const obj = { items: ["a", "b", "c"] };
            expect(encodeAsFormParameter(obj)).toEqual({
                "items[0]": "a",
                "items[1]": "b",
                "items[2]": "c",
            });
        });

        it("should handle empty arrays", () => {
            const obj = { items: [] };
            expect(encodeAsFormParameter(obj)).toEqual({});
        });

        it("should handle arrays with mixed types", () => {
            const obj = { mixed: ["string", 42, true, false] };
            expect(encodeAsFormParameter(obj)).toEqual({
                "mixed[0]": "string",
                "mixed[1]": "42",
                "mixed[2]": "true",
                "mixed[3]": "false",
            });
        });

        it("should handle arrays with objects", () => {
            const obj = { users: [{ name: "John" }, { name: "Jane" }] };
            expect(encodeAsFormParameter(obj)).toEqual({
                "users[0][name]": "John",
                "users[1][name]": "Jane",
            });
        });

        it("should handle arrays with null/undefined values", () => {
            const obj = { items: ["a", null, "c", undefined, "e"] };
            expect(encodeAsFormParameter(obj)).toEqual({
                "items[0]": "a",
                "items[1]": "",
                "items[2]": "c",
                "items[4]": "e",
            });
        });
    });

    describe("Nested objects", () => {
        it("should handle nested objects", () => {
            const obj = { user: { name: "John", age: 30 } };
            expect(encodeAsFormParameter(obj)).toEqual({
                "user[name]": "John",
                "user[age]": "30",
            });
        });

        it("should handle deeply nested objects", () => {
            const obj = { user: { profile: { name: "John", settings: { theme: "dark" } } } };
            expect(encodeAsFormParameter(obj)).toEqual({
                "user[profile][name]": "John",
                "user[profile][settings][theme]": "dark",
            });
        });

        it("should handle empty nested objects", () => {
            const obj = { user: {} };
            expect(encodeAsFormParameter(obj)).toEqual({});
        });
    });

    describe("Special characters and encoding", () => {
        it("should not encode values (encode: false is used)", () => {
            const obj = { name: "John Doe", email: "john@example.com" };
            expect(encodeAsFormParameter(obj)).toEqual({
                name: "John Doe",
                email: "john@example.com",
            });
        });

        it("should not encode special characters in keys", () => {
            const obj = { "user name": "John", "email[primary]": "john@example.com" };
            expect(encodeAsFormParameter(obj)).toEqual({
                "user name": "John",
                "email[primary]": "john@example.com",
            });
        });

        it("should handle values that contain special characters", () => {
            const obj = {
                query: "search term with spaces",
                filter: "category:electronics",
            };
            expect(encodeAsFormParameter(obj)).toEqual({
                query: "search term with spaces",
                filter: "category:electronics",
            });
        });

        it("should handle ampersand and equals characters (edge case)", () => {
            // Note: Values containing & and = may be problematic because
            // encodeAsFormParameter splits on these characters when parsing the stringified result
            const obj = {
                message: "Hello & welcome",
                equation: "x = y + z",
            };
            // This demonstrates the limitation - ampersands and equals signs in values
            // will cause the parameter to be split incorrectly
            const result = encodeAsFormParameter(obj);

            // We expect this to be parsed incorrectly due to the implementation
            expect(result.message).toBe("Hello ");
            expect(result[" welcome"]).toBeUndefined();
            expect(result.equation).toBe("x ");
            expect(result[" y + z"]).toBeUndefined();
        });
    });

    describe("Form data specific scenarios", () => {
        it("should handle file upload metadata", () => {
            const metadata = {
                file: {
                    name: "document.pdf",
                    size: 1024,
                    type: "application/pdf",
                },
                options: {
                    compress: true,
                    quality: 0.8,
                },
            };
            expect(encodeAsFormParameter(metadata)).toEqual({
                "file[name]": "document.pdf",
                "file[size]": "1024",
                "file[type]": "application/pdf",
                "options[compress]": "true",
                "options[quality]": "0.8",
            });
        });

        it("should handle form validation data", () => {
            const formData = {
                fields: ["name", "email", "phone"],
                validation: {
                    required: ["name", "email"],
                    patterns: {
                        email: "^[^@]+@[^@]+\\.[^@]+$",
                        phone: "^\\+?[1-9]\\d{1,14}$",
                    },
                },
            };
            expect(encodeAsFormParameter(formData)).toEqual({
                "fields[0]": "name",
                "fields[1]": "email",
                "fields[2]": "phone",
                "validation[required][0]": "name",
                "validation[required][1]": "email",
                "validation[patterns][email]": "^[^@]+@[^@]+\\.[^@]+$",
                "validation[patterns][phone]": "^\\+?[1-9]\\d{1,14}$",
            });
        });

        it("should handle search/filter parameters", () => {
            const searchParams = {
                filters: {
                    status: ["active", "pending"],
                    category: {
                        type: "electronics",
                        subcategories: ["phones", "laptops"],
                    },
                },
                sort: { field: "name", direction: "asc" },
                pagination: { page: 1, limit: 20 },
            };
            expect(encodeAsFormParameter(searchParams)).toEqual({
                "filters[status][0]": "active",
                "filters[status][1]": "pending",
                "filters[category][type]": "electronics",
                "filters[category][subcategories][0]": "phones",
                "filters[category][subcategories][1]": "laptops",
                "sort[field]": "name",
                "sort[direction]": "asc",
                "pagination[page]": "1",
                "pagination[limit]": "20",
            });
        });
    });

    describe("Edge cases", () => {
        it("should handle boolean values", () => {
            const obj = { enabled: true, disabled: false };
            expect(encodeAsFormParameter(obj)).toEqual({
                enabled: "true",
                disabled: "false",
            });
        });

        it("should handle empty strings", () => {
            const obj = { name: "", description: "test" };
            expect(encodeAsFormParameter(obj)).toEqual({
                name: "",
                description: "test",
            });
        });

        it("should handle zero values", () => {
            const obj = { count: 0, price: 0.0 };
            expect(encodeAsFormParameter(obj)).toEqual({
                count: "0",
                price: "0",
            });
        });

        it("should handle numeric keys", () => {
            const obj = { "0": "zero", "1": "one" };
            expect(encodeAsFormParameter(obj)).toEqual({
                "0": "zero",
                "1": "one",
            });
        });

        it("should handle objects with null/undefined values", () => {
            const obj = { name: "John", age: null, email: undefined, active: true };
            expect(encodeAsFormParameter(obj)).toEqual({
                name: "John",
                age: "",
                active: "true",
            });
        });
    });

    describe("Integration with form submission", () => {
        it("should produce form-compatible key-value pairs", () => {
            const formObject = {
                username: "john_doe",
                preferences: {
                    theme: "dark",
                    notifications: ["email", "push"],
                    settings: {
                        autoSave: true,
                        timeout: 300,
                    },
                },
            };

            const result = encodeAsFormParameter(formObject);

            // Verify all values are strings (as required for form data)
            Object.values(result).forEach((value) => {
                expect(typeof value).toBe("string");
            });

            // Verify the structure can be reconstructed
            expect(result).toEqual({
                username: "john_doe",
                "preferences[theme]": "dark",
                "preferences[notifications][0]": "email",
                "preferences[notifications][1]": "push",
                "preferences[settings][autoSave]": "true",
                "preferences[settings][timeout]": "300",
            });
        });

        it("should handle complex nested arrays for API parameters", () => {
            const apiParams = {
                query: {
                    filters: [
                        { field: "status", operator: "eq", value: "active" },
                        { field: "created", operator: "gte", value: "2023-01-01" },
                    ],
                    sort: [
                        { field: "name", direction: "asc" },
                        { field: "created", direction: "desc" },
                    ],
                },
            };

            const result = encodeAsFormParameter(apiParams);
            expect(result).toEqual({
                "query[filters][0][field]": "status",
                "query[filters][0][operator]": "eq",
                "query[filters][0][value]": "active",
                "query[filters][1][field]": "created",
                "query[filters][1][operator]": "gte",
                "query[filters][1][value]": "2023-01-01",
                "query[sort][0][field]": "name",
                "query[sort][0][direction]": "asc",
                "query[sort][1][field]": "created",
                "query[sort][1][direction]": "desc",
            });
        });
    });

    describe("Error cases and malformed input", () => {
        it("should handle circular references gracefully", () => {
            const obj: any = { name: "test" };
            obj.self = obj;

            // This will throw a RangeError due to stack overflow - this is expected behavior
            expect(() => encodeAsFormParameter(obj)).toThrow("Maximum call stack size exceeded");
        });

        it("should handle very deeply nested objects", () => {
            let deepObj: any = { value: "deep" };
            for (let i = 0; i < 100; i++) {
                deepObj = { level: deepObj };
            }

            expect(() => encodeAsFormParameter(deepObj)).not.toThrow();
            const result = encodeAsFormParameter(deepObj);
            expect(Object.keys(result).length).toBeGreaterThan(0);
        });

        it("should handle empty string splitting edge case", () => {
            // Test what happens when qs returns an empty string
            const result = encodeAsFormParameter({});
            expect(result).toEqual({});
        });
    });
});
