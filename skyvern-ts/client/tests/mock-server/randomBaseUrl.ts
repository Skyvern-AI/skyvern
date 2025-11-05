export function randomBaseUrl(): string {
    const randomString = Math.random().toString(36).substring(2, 15);
    return `http://${randomString}.localhost`;
}
