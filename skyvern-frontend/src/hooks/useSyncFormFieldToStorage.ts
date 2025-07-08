import { useEffect } from "react";
import { UseFormReturn, FieldValues, WatchObserver } from "react-hook-form";

/**
 * Syncs a form field value to localStorage whenever it changes.
 * @param form - A react-hook-form object with a .watch method
 * @param fieldName - The name of the field to watch
 * @param storageKey - The key to write to in localStorage
 */
export function useSyncFormFieldToStorage<T extends FieldValues>(
  form: UseFormReturn<T>,
  fieldName: keyof T & string,
  storageKey: string,
) {
  useEffect(() => {
    const subscription = form.watch(((value, { name }) => {
      if (name === fieldName && typeof value[fieldName] === "string") {
        localStorage.setItem(storageKey, value[fieldName] as string);
      }
    }) as WatchObserver<T>);
    return () => subscription.unsubscribe();
  }, [form, fieldName, storageKey]);
}
