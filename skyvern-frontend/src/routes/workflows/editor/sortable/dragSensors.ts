import {
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type KeyboardCoordinateGetter,
  type SensorDescriptor,
  type SensorOptions,
} from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";

// Clicking the grip handle to focus it must not register as a drag start;
// 5 px keeps the focus path clean while still feeling responsive.
export const POINTER_ACTIVATION_DISTANCE_PX = 5;

export function useDragSensors(
  keyboardCoordinateGetter: KeyboardCoordinateGetter = sortableKeyboardCoordinates,
): SensorDescriptor<SensorOptions>[] {
  return useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: POINTER_ACTIVATION_DISTANCE_PX },
    }),
    useSensor(KeyboardSensor, {
      // Without sortableKeyboardCoordinates the default getter steps by a
      // fixed pixel delta and skips slots in variable-height chains. Callers
      // may pass a wrapper to add scope-aware filtering (see
      // `createScopeAwareKeyboardCoordinates`) so arrow-key reorders inside a
      // loop/conditional only consider in-scope siblings.
      coordinateGetter: keyboardCoordinateGetter,
    }),
  );
}
