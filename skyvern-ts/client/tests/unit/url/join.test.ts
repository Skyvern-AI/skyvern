import { join } from "../../../src/core/url/index";

describe("join", () => {
    describe("basic functionality", () => {
        it("should return empty string for empty base", () => {
            expect(join("")).toBe("");
            expect(join("", "path")).toBe("");
        });

        it("should handle single segment", () => {
            expect(join("base", "segment")).toBe("base/segment");
            expect(join("base/", "segment")).toBe("base/segment");
            expect(join("base", "/segment")).toBe("base/segment");
            expect(join("base/", "/segment")).toBe("base/segment");
        });

        it("should handle multiple segments", () => {
            expect(join("base", "path1", "path2", "path3")).toBe("base/path1/path2/path3");
            expect(join("base/", "/path1/", "/path2/", "/path3/")).toBe("base/path1/path2/path3/");
        });
    });

    describe("URL handling", () => {
        it("should handle absolute URLs", () => {
            expect(join("https://example.com", "api", "v1")).toBe("https://example.com/api/v1");
            expect(join("https://example.com/", "/api/", "/v1/")).toBe("https://example.com/api/v1/");
            expect(join("https://example.com/base", "api", "v1")).toBe("https://example.com/base/api/v1");
        });

        it("should preserve URL query parameters and fragments", () => {
            expect(join("https://example.com?query=1", "api")).toBe("https://example.com/api?query=1");
            expect(join("https://example.com#fragment", "api")).toBe("https://example.com/api#fragment");
            expect(join("https://example.com?query=1#fragment", "api")).toBe(
                "https://example.com/api?query=1#fragment",
            );
        });

        it("should handle different protocols", () => {
            expect(join("http://example.com", "api")).toBe("http://example.com/api");
            expect(join("ftp://example.com", "files")).toBe("ftp://example.com/files");
            expect(join("ws://example.com", "socket")).toBe("ws://example.com/socket");
        });

        it("should fallback to path joining for malformed URLs", () => {
            expect(join("not-a-url://", "path")).toBe("not-a-url:///path");
        });
    });

    describe("edge cases", () => {
        it("should handle empty segments", () => {
            expect(join("base", "", "path")).toBe("base/path");
            expect(join("base", null as any, "path")).toBe("base/path");
            expect(join("base", undefined as any, "path")).toBe("base/path");
        });

        it("should handle segments with only slashes", () => {
            expect(join("base", "/", "path")).toBe("base/path");
            expect(join("base", "//", "path")).toBe("base/path");
        });

        it("should handle base paths with trailing slashes", () => {
            expect(join("base/", "path")).toBe("base/path");
        });

        it("should handle complex nested paths", () => {
            expect(join("api/v1/", "/users/", "/123/", "/profile")).toBe("api/v1/users/123/profile");
        });
    });

    describe("real-world scenarios", () => {
        it("should handle API endpoint construction", () => {
            const baseUrl = "https://api.example.com/v1";
            expect(join(baseUrl, "users", "123", "posts")).toBe("https://api.example.com/v1/users/123/posts");
        });

        it("should handle file path construction", () => {
            expect(join("/var/www", "html", "assets", "images")).toBe("/var/www/html/assets/images");
        });

        it("should handle relative path construction", () => {
            expect(join("../parent", "child", "grandchild")).toBe("../parent/child/grandchild");
        });

        it("should handle Windows-style paths", () => {
            expect(join("C:\\Users", "Documents", "file.txt")).toBe("C:\\Users/Documents/file.txt");
        });
    });

    describe("performance scenarios", () => {
        it("should handle many segments efficiently", () => {
            const segments = Array(100).fill("segment");
            const result = join("base", ...segments);
            expect(result).toBe(`base/${segments.join("/")}`);
        });

        it("should handle long URLs", () => {
            const longPath = "a".repeat(1000);
            expect(join("https://example.com", longPath)).toBe(`https://example.com/${longPath}`);
        });
    });

    describe("trailing slash preservation", () => {
        it("should preserve trailing slash on final result when base has trailing slash and no segments", () => {
            expect(join("https://api.example.com/")).toBe("https://api.example.com/");
            expect(join("https://api.example.com/v1/")).toBe("https://api.example.com/v1/");
        });

        it("should preserve trailing slash when last segment has trailing slash", () => {
            expect(join("https://api.example.com", "users/")).toBe("https://api.example.com/users/");
            expect(join("api/v1", "users/")).toBe("api/v1/users/");
        });

        it("should preserve trailing slash with multiple segments where last has trailing slash", () => {
            expect(join("https://api.example.com", "v1", "collections/")).toBe(
                "https://api.example.com/v1/collections/",
            );
            expect(join("base", "path1", "path2/")).toBe("base/path1/path2/");
        });
    });
});
