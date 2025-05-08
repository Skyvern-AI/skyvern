import io
from typing import TypedDict

from PIL import Image


class Resolution(TypedDict):
    width: int
    height: int


MAX_SCALING_TARGETS_ANTHROPIC_CUA: dict[str, Resolution] = {
    "XGA": Resolution(width=1024, height=768),  # 4:3
    "WXGA": Resolution(width=1280, height=800),  # 16:10
    "FWXGA": Resolution(width=1366, height=768),  # ~16:9
}


def get_resize_target_dimension(
    window_size: Resolution, max_scaling_targets: dict[str, Resolution] = MAX_SCALING_TARGETS_ANTHROPIC_CUA
) -> Resolution:
    ratio = window_size["width"] / window_size["height"]
    for dimension in max_scaling_targets.values():
        if abs(dimension["width"] / dimension["height"] - ratio) < 0.02:
            if dimension["width"] < window_size["width"]:
                # we only return the dimension if it's smaller than the window size
                return dimension
    return window_size


def resize_screenshots(screenshots: list[bytes], target_dimension: Resolution) -> list[bytes]:
    """
    The image scaling logic is originated from anthropic's quickstart guide:
    https://github.com/anthropics/anthropic-quickstarts/blob/81c4085944abb1734db411f05290b538fdc46dcd/computer-use-demo/computer_use_demo/tools/computer.py#L49-L60
    """
    new_screenshots = []
    for screenshot in screenshots:
        # Convert bytes to PIL Image
        img = Image.open(io.BytesIO(screenshot))

        # Resize image to target dimensions
        resized_img = img.resize((target_dimension["width"], target_dimension["height"]), Image.Resampling.LANCZOS)

        # Convert back to bytes
        img_byte_arr = io.BytesIO()
        resized_img.save(img_byte_arr, format="PNG")
        img_byte = img_byte_arr.getvalue()

        new_screenshots.append(img_byte)
    return new_screenshots


def scale_coordinates(
    current_coordinates: tuple[int, int],
    current_dimension: Resolution,
    target_dimension: Resolution,
) -> tuple[int, int]:
    return (
        int(current_coordinates[0] * target_dimension["width"] / current_dimension["width"]),
        int(current_coordinates[1] * target_dimension["height"] / current_dimension["height"]),
    )
