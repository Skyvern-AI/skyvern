from __future__ import annotations

import argparse
from collections.abc import Iterable

DEFAULT_TESSERACT_OCR_LANGUAGE_PACKS = (
    "eng",
    "spa",
    "fra",
    "deu",
    "ita",
    "por",
    "nld",
    "pol",
    "rus",
    "ukr",
    "ara",
    "chi-sim",
    "chi-tra",
    "jpn",
    "kor",
    "hin",
)


def tesseract_ocr_packages(language_packs: Iterable[str] = DEFAULT_TESSERACT_OCR_LANGUAGE_PACKS) -> list[str]:
    return [f"tesseract-ocr-{language_pack}" for language_pack in language_packs]


def tesseract_language_arg(language_packs: Iterable[str] = DEFAULT_TESSERACT_OCR_LANGUAGE_PACKS) -> str:
    return "+".join(language_pack.replace("-", "_") for language_pack in language_packs)


DEFAULT_FLAT_FILL_OCR_LANGUAGES = tesseract_language_arg()


def main() -> None:
    parser = argparse.ArgumentParser(description="Print Tesseract language package metadata.")
    parser.add_argument(
        "--apt-packages",
        action="store_true",
        help="Print Debian OCR language packages for apt-get install.",
    )
    parser.add_argument(
        "--tesseract-languages",
        action="store_true",
        help="Print the Tesseract -l language argument.",
    )
    args = parser.parse_args()

    if args.apt_packages:
        print(" ".join(tesseract_ocr_packages()))
        return
    if args.tesseract_languages:
        print(DEFAULT_FLAT_FILL_OCR_LANGUAGES)
        return
    parser.error("expected --apt-packages or --tesseract-languages")


if __name__ == "__main__":
    main()
