import { toQueryString } from "../../../src/core/url/index";

describe("Test qs toQueryString", () => {
    describe("Basic functionality", () => {
        it("should return empty string for null/undefined", () => {
            expect(toQueryString(null)).toBe("");
            expect(toQueryString(undefined)).toBe("");
        });

        it("should return empty string for primitive values", () => {
            expect(toQueryString("hello")).toBe("");
            expect(toQueryString(42)).toBe("");
            expect(toQueryString(true)).toBe("");
            expect(toQueryString(false)).toBe("");
        });

        it("should handle empty objects", () => {
            expect(toQueryString({})).toBe("");
        });

        it("should handle simple key-value pairs", () => {
            const obj = { name: "John", age: 30 };
            expect(toQueryString(obj)).toBe("name=John&age=30");
        });
    });

    describe("Array handling", () => {
        it("should handle arrays with indices format (default)", () => {
            const obj = { items: ["a", "b", "c"] };
            expect(toQueryString(obj)).toBe("items%5B0%5D=a&items%5B1%5D=b&items%5B2%5D=c");
        });

        it("should handle arrays with repeat format", () => {
            const obj = { items: ["a", "b", "c"] };
            expect(toQueryString(obj, { arrayFormat: "repeat" })).toBe("items=a&items=b&items=c");
        });

        it("should handle empty arrays", () => {
            const obj = { items: [] };
            expect(toQueryString(obj)).toBe("");
        });

        it("should handle arrays with mixed types", () => {
            const obj = { mixed: ["string", 42, true, false] };
            expect(toQueryString(obj)).toBe("mixed%5B0%5D=string&mixed%5B1%5D=42&mixed%5B2%5D=true&mixed%5B3%5D=false");
        });

        it("should handle arrays with objects", () => {
            const obj = { users: [{ name: "John" }, { name: "Jane" }] };
            expect(toQueryString(obj)).toBe("users%5B0%5D%5Bname%5D=John&users%5B1%5D%5Bname%5D=Jane");
        });

        it("should handle arrays with objects in repeat format", () => {
            const obj = { users: [{ name: "John" }, { name: "Jane" }] };
            expect(toQueryString(obj, { arrayFormat: "repeat" })).toBe("users%5Bname%5D=John&users%5Bname%5D=Jane");
        });
    });

    describe("Nested objects", () => {
        it("should handle nested objects", () => {
            const obj = { user: { name: "John", age: 30 } };
            expect(toQueryString(obj)).toBe("user%5Bname%5D=John&user%5Bage%5D=30");
        });

        it("should handle deeply nested objects", () => {
            const obj = { user: { profile: { name: "John", settings: { theme: "dark" } } } };
            expect(toQueryString(obj)).toBe(
                "user%5Bprofile%5D%5Bname%5D=John&user%5Bprofile%5D%5Bsettings%5D%5Btheme%5D=dark",
            );
        });

        it("should handle empty nested objects", () => {
            const obj = { user: {} };
            expect(toQueryString(obj)).toBe("");
        });
    });

    describe("Encoding", () => {
        it("should encode by default", () => {
            const obj = { name: "John Doe", email: "john@example.com" };
            expect(toQueryString(obj)).toBe("name=John%20Doe&email=john%40example.com");
        });

        it("should not encode when encode is false", () => {
            const obj = { name: "John Doe", email: "john@example.com" };
            expect(toQueryString(obj, { encode: false })).toBe("name=John Doe&email=john@example.com");
        });

        it("should encode special characters in keys", () => {
            const obj = { "user name": "John", "email[primary]": "john@example.com" };
            expect(toQueryString(obj)).toBe("user%20name=John&email%5Bprimary%5D=john%40example.com");
        });

        it("should not encode special characters in keys when encode is false", () => {
            const obj = { "user name": "John", "email[primary]": "john@example.com" };
            expect(toQueryString(obj, { encode: false })).toBe("user name=John&email[primary]=john@example.com");
        });
    });

    describe("Mixed scenarios", () => {
        it("should handle complex nested structures", () => {
            const obj = {
                filters: {
                    status: ["active", "pending"],
                    category: {
                        type: "electronics",
                        subcategories: ["phones", "laptops"],
                    },
                },
                sort: { field: "name", direction: "asc" },
            };
            expect(toQueryString(obj)).toBe(
                "filters%5Bstatus%5D%5B0%5D=active&filters%5Bstatus%5D%5B1%5D=pending&filters%5Bcategory%5D%5Btype%5D=electronics&filters%5Bcategory%5D%5Bsubcategories%5D%5B0%5D=phones&filters%5Bcategory%5D%5Bsubcategories%5D%5B1%5D=laptops&sort%5Bfield%5D=name&sort%5Bdirection%5D=asc",
            );
        });

        it("should handle complex nested structures with repeat format", () => {
            const obj = {
                filters: {
                    status: ["active", "pending"],
                    category: {
                        type: "electronics",
                        subcategories: ["phones", "laptops"],
                    },
                },
                sort: { field: "name", direction: "asc" },
            };
            expect(toQueryString(obj, { arrayFormat: "repeat" })).toBe(
                "filters%5Bstatus%5D=active&filters%5Bstatus%5D=pending&filters%5Bcategory%5D%5Btype%5D=electronics&filters%5Bcategory%5D%5Bsubcategories%5D=phones&filters%5Bcategory%5D%5Bsubcategories%5D=laptops&sort%5Bfield%5D=name&sort%5Bdirection%5D=asc",
            );
        });

        it("should handle arrays with null/undefined values", () => {
            const obj = { items: ["a", null, "c", undefined, "e"] };
            expect(toQueryString(obj)).toBe("items%5B0%5D=a&items%5B1%5D=&items%5B2%5D=c&items%5B4%5D=e");
        });

        it("should handle objects with null/undefined values", () => {
            const obj = { name: "John", age: null, email: undefined, active: true };
            expect(toQueryString(obj)).toBe("name=John&age=&active=true");
        });
    });

    describe("Edge cases", () => {
        it("should handle numeric keys", () => {
            const obj = { "0": "zero", "1": "one" };
            expect(toQueryString(obj)).toBe("0=zero&1=one");
        });

        it("should handle boolean values in objects", () => {
            const obj = { enabled: true, disabled: false };
            expect(toQueryString(obj)).toBe("enabled=true&disabled=false");
        });

        it("should handle empty strings", () => {
            const obj = { name: "", description: "test" };
            expect(toQueryString(obj)).toBe("name=&description=test");
        });

        it("should handle zero values", () => {
            const obj = { count: 0, price: 0.0 };
            expect(toQueryString(obj)).toBe("count=0&price=0");
        });

        it("should handle arrays with empty strings", () => {
            const obj = { items: ["a", "", "c"] };
            expect(toQueryString(obj)).toBe("items%5B0%5D=a&items%5B1%5D=&items%5B2%5D=c");
        });
    });

    describe("Options combinations", () => {
        it("should respect both arrayFormat and encode options", () => {
            const obj = { items: ["a & b", "c & d"] };
            expect(toQueryString(obj, { arrayFormat: "repeat", encode: false })).toBe("items=a & b&items=c & d");
        });

        it("should use default options when none provided", () => {
            const obj = { items: ["a", "b"] };
            expect(toQueryString(obj)).toBe("items%5B0%5D=a&items%5B1%5D=b");
        });

        it("should merge provided options with defaults", () => {
            const obj = { items: ["a", "b"], name: "John Doe" };
            expect(toQueryString(obj, { encode: false })).toBe("items[0]=a&items[1]=b&name=John Doe");
        });
    });
});
