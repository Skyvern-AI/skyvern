export async function getFetchFn(): Promise<typeof fetch> {
    return fetch;
}
