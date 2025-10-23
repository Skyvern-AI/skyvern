export function stringifyValidationError(error) {
    if (error.path.length === 0) {
        return error.message;
    }
    return `${error.path.join(" -> ")}: ${error.message}`;
}
