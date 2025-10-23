export function partition(items, predicate) {
    const trueItems = [], falseItems = [];
    for (const item of items) {
        if (predicate(item)) {
            trueItems.push(item);
        }
        else {
            falseItems.push(item);
        }
    }
    return [trueItems, falseItems];
}
