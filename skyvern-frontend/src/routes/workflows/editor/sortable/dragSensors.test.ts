// @vitest-environment jsdom

import { describe, expect, test } from "vitest";
import { renderHook } from "@testing-library/react";
import {
  KeyboardSensor,
  PointerSensor,
  type KeyboardSensorOptions,
  type PointerSensorOptions,
} from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";

import { POINTER_ACTIVATION_DISTANCE_PX, useDragSensors } from "./dragSensors";

describe("useDragSensors (SKY-9063)", () => {
  test("registers both PointerSensor and KeyboardSensor", () => {
    // SKY-9063 AC hinges on keyboard parity with pointer drag — if either
    // sensor is missing, one of the two input paths silently degrades to
    // no-op. Asserting both are present guards the full reorder path.
    const { result } = renderHook(() => useDragSensors());

    const sensorClasses = result.current.map((descriptor) => descriptor.sensor);
    expect(sensorClasses).toContain(PointerSensor);
    expect(sensorClasses).toContain(KeyboardSensor);
  });

  test("pairs the KeyboardSensor with sortableKeyboardCoordinates", () => {
    // `sortableKeyboardCoordinates` is what turns ↑/↓ key presses into a
    // move across the current SortableContext's sibling slots. Using the
    // default coordinate getter instead would step by a fixed pixel delta
    // and break reorder inside variable-height block chains, so we assert
    // the pairing explicitly.
    const { result } = renderHook(() => useDragSensors());

    const keyboardDescriptor = result.current.find(
      (descriptor) => descriptor.sensor === KeyboardSensor,
    );
    expect(keyboardDescriptor).toBeDefined();
    const keyboardOptions = keyboardDescriptor?.options as
      | KeyboardSensorOptions
      | undefined;
    expect(keyboardOptions?.coordinateGetter).toBe(sortableKeyboardCoordinates);
  });

  test("keeps the 5px PointerSensor activation distance from the pre-keyboard wiring", () => {
    // The PointerSensor threshold predates SKY-9063; carrying it through
    // the extracted helper unchanged means a click on the grip handle
    // still focuses the button (enabling keyboard activation) rather than
    // being swallowed as an immediate drag start.
    const { result } = renderHook(() => useDragSensors());

    const pointerDescriptor = result.current.find(
      (descriptor) => descriptor.sensor === PointerSensor,
    );
    expect(pointerDescriptor).toBeDefined();
    const pointerOptions = pointerDescriptor?.options as
      | PointerSensorOptions
      | undefined;
    expect(pointerOptions?.activationConstraint).toEqual({
      distance: POINTER_ACTIVATION_DISTANCE_PX,
    });
  });
});
