// borrowed from https://github.com/lodash/lodash/blob/master/isPlainObject.js
export function isPlainObject(value) {
    if (typeof value !== "object" || value === null) {
        return false;
    }
    if (Object.getPrototypeOf(value) === null) {
        return true;
    }
    let proto = value;
    while (Object.getPrototypeOf(proto) !== null) {
        proto = Object.getPrototypeOf(proto);
    }
    return Object.getPrototypeOf(value) === proto;
}
