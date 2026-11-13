from __future__ import annotations

import hashlib
import json
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, NotRequired, TypedDict, final

import structlog
import yaml
from pydantic import JsonValue
from typing_extensions import TypeIs

LOG = structlog.get_logger()

MAX_PACKAGE_BYTES = 1_048_576
MAX_PACKAGE_FILES = 128
MAX_SKILL_RESULT_CHARS = 19_000
SKILL_FILE = "SKILL.md"
LOAD_SKILL_TOOL_NAME = "load_skill"
READ_SKILL_RESOURCE_TOOL_NAME = "read_skill_resource"
SKILL_TOOL_NAMES = frozenset({LOAD_SKILL_TOOL_NAME, READ_SKILL_RESOURCE_TOOL_NAME})
EVERYONE_SKILL_LABEL = "a Skyvern skill"

SkillOwner = Literal["everyone", "organization", "workflow"]
SkillError = Literal["unknown_skill", "unknown_file"]

_OWNER_RANK: dict[SkillOwner, int] = {"everyone": 0, "organization": 1, "workflow": 2}
_NAME_CHARACTERS = frozenset(string.ascii_lowercase + string.digits + "-")


@final
class SkillLoaded(TypedDict):
    name: str
    version: str
    body: str
    files: list[str]
    owner: NotRequired[SkillOwner | None]


@final
class SkillAlreadyLoaded(TypedDict):
    status: Literal["already_loaded"]
    name: str
    version: str
    owner: NotRequired[SkillOwner | None]


@final
class SkillResource(TypedDict):
    name: str
    path: str
    text: str
    owner: NotRequired[SkillOwner | None]


@final
class SkillRefusal(TypedDict):
    error: SkillError
    message: str
    names: NotRequired[list[str]]
    files: NotRequired[list[str]]


SkillLoadResult = SkillLoaded | SkillAlreadyLoaded | SkillRefusal
SkillReadResult = SkillResource | SkillRefusal


def is_refusal(result: SkillLoadResult | SkillReadResult) -> TypeIs[SkillRefusal]:
    return "error" in result


def skill_result_chars(result: SkillLoadResult | SkillReadResult) -> int:
    return len(json.dumps({"status": "completed", "output": result, "error": None}))


def _relative_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or parsed.as_posix() != path or ".." in parsed.parts or "\\" in path:
        raise ValueError("Skill files must use canonical relative paths inside the package.")


@dataclass(frozen=True)
class SkillFile:
    path: str
    content: str


@dataclass(frozen=True)
class SkillPackage:
    files: tuple[SkillFile, ...]
    name: str = field(init=False)
    description: str = field(init=False)
    body: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.files, tuple) or not 1 <= len(self.files) <= MAX_PACKAGE_FILES:
            raise ValueError(f"A skill package needs 1 to {MAX_PACKAGE_FILES} files.")
        contents: dict[str, str] = {}
        for item in self.files:
            if not isinstance(item, SkillFile) or not isinstance(item.content, str):
                raise TypeError("Skill packages hold text files only.")
            _relative_path(item.path)
            if item.path in contents:
                raise ValueError("Duplicate skill file path.")
            contents[item.path] = item.content
        if sum(len(content.encode("utf-8")) for content in contents.values()) > MAX_PACKAGE_BYTES:
            raise ValueError(f"Skill package exceeds {MAX_PACKAGE_BYTES} bytes.")
        skill_md = contents.get(SKILL_FILE)
        if skill_md is None:
            raise ValueError("A skill package requires SKILL.md.")
        lines = skill_md.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ValueError("SKILL.md requires YAML frontmatter.")
        closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if closing is None:
            raise ValueError("SKILL.md frontmatter is not closed.")
        try:
            metadata = yaml.safe_load("".join(lines[1:closing]))
        except yaml.YAMLError as error:
            raise ValueError("SKILL.md frontmatter is invalid YAML.") from error
        if not isinstance(metadata, dict):
            raise TypeError("SKILL.md frontmatter must be a mapping.")
        name = metadata.get("name")
        description = metadata.get("description")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 64
            or not set(name) <= _NAME_CHARACTERS
            or name.startswith("-")
            or name.endswith("-")
            or "--" in name
        ):
            raise ValueError("SKILL.md name must follow the Agent Skills naming constraints.")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > 1024
            or "\n" in description
            or "\r" in description
        ):
            raise ValueError("SKILL.md description must be one line of 1-1024 characters.")
        canonical = json.dumps(contents, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "body", "".join(lines[closing + 1 :]))
        object.__setattr__(self, "content_hash", hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self._check_result_sizes(contents)

    def _check_result_sizes(self, contents: Mapping[str, str]) -> None:
        loaded = SkillLoaded(name=self.name, version=self.version, body=self.body, files=self.resource_paths)
        if skill_result_chars(loaded) > MAX_SKILL_RESULT_CHARS:
            raise ValueError(f"SKILL.md of {self.name} exceeds {MAX_SKILL_RESULT_CHARS} result characters.")
        for path, text in contents.items():
            if skill_result_chars(SkillResource(name=self.name, path=path, text=text)) > MAX_SKILL_RESULT_CHARS:
                raise ValueError(f"{path} of {self.name} exceeds {MAX_SKILL_RESULT_CHARS} result characters.")

    @property
    def version(self) -> str:
        return self.content_hash[:12]

    @property
    def resource_paths(self) -> list[str]:
        return [item.path for item in self.files if item.path != SKILL_FILE]

    @classmethod
    def from_files(cls, files: Mapping[str, str]) -> SkillPackage:
        return cls(tuple(SkillFile(path, content) for path, content in sorted(files.items())))

    @classmethod
    def from_directory(cls, directory: Path) -> SkillPackage:
        if directory.is_symlink():
            raise ValueError("Skill packages cannot contain symbolic links.")
        root = directory.resolve(strict=True)
        paths: list[Path] = []
        for path in sorted(root.rglob("*")):
            if any(part == "__pycache__" or part.startswith(".") for part in path.relative_to(root).parts):
                continue
            if path.is_symlink():
                raise ValueError("Skill packages cannot contain symbolic links.")
            if path.is_file():
                paths.append(path)
        if len(paths) > MAX_PACKAGE_FILES:
            raise ValueError(f"A skill package needs 1 to {MAX_PACKAGE_FILES} files.")
        if sum(path.stat().st_size for path in paths) > MAX_PACKAGE_BYTES:
            raise ValueError(f"Skill package exceeds {MAX_PACKAGE_BYTES} bytes.")
        package = cls.from_files(
            {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8") for path in paths}
        )
        if root.name != package.name:
            raise ValueError("The skill directory must match its frontmatter name.")
        return package

    def read(self, path: str) -> str | None:
        return next((item.content for item in self.files if item.path == path), None)


@dataclass(frozen=True)
class AvailableSkill:
    package: SkillPackage
    owner: SkillOwner
    version: str


class SkillSession:
    def __init__(
        self,
        skills: Sequence[AvailableSkill],
        *,
        agent: str,
        **log_fields: str | None,
    ) -> None:
        winners: dict[str, AvailableSkill] = {}
        for skill in skills:
            prior = winners.get(skill.package.name)
            if prior is None or _OWNER_RANK[skill.owner] > _OWNER_RANK[prior.owner]:
                winners[skill.package.name] = skill
        self._skills = dict(sorted(winners.items()))
        self._loaded: set[tuple[str, str]] = set()
        self.agent = agent
        self.log_fields = log_fields

    def catalog_lines(self) -> list[str]:
        return [f"{name} [{skill.owner}]: {skill.package.description}" for name, skill in self._skills.items()]

    def owner(self, name: str) -> SkillOwner | None:
        skill = self._skills.get(name)
        return skill.owner if skill else None

    def shows_name(self, name: JsonValue) -> bool:
        return isinstance(name, str) and self.owner(name) in ("organization", "workflow")

    def public_arguments(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        if self.shows_name(arguments.get("name")):
            return dict(arguments)
        return {**{key: value for key, value in arguments.items() if key != "path"}, "name": EVERYONE_SKILL_LABEL}

    def load(self, name: str) -> SkillLoadResult:
        result = self._load(name)
        chars = len(result["body"]) if "body" in result else 0
        self._log("agent_skill_loaded", name, None, result, chars, already_loaded="status" in result)
        return result

    def read_resource(self, name: str, path: str) -> SkillReadResult:
        result = self._read_resource(name, path)
        chars = len(result["text"]) if "text" in result else 0
        self._log("agent_skill_resource_read", name, path, result, chars)
        return result

    def _log(
        self,
        event: str,
        name: str,
        path: str | None,
        result: SkillLoadResult | SkillReadResult,
        chars: int,
        **extra: bool,
    ) -> None:
        skill = self._skills.get(name)
        LOG.info(
            event,
            agent=self.agent,
            **self.log_fields,
            name=name,
            owner=skill.owner if skill else None,
            # `version` is taken: add_log_context overwrites it with the app's build version.
            skill_version=skill.version if skill else None,
            path=path,
            chars=chars,
            error=result["error"] if is_refusal(result) else None,
            **extra,
        )

    def _load(self, name: str) -> SkillLoadResult:
        skill = self._skills.get(name)
        if skill is None:
            return self._unknown_skill(name)
        if (name, skill.version) in self._loaded:
            return SkillAlreadyLoaded(status="already_loaded", name=name, version=skill.version)
        self._loaded.add((name, skill.version))
        return SkillLoaded(
            name=name, version=skill.version, body=skill.package.body, files=skill.package.resource_paths
        )

    def _read_resource(self, name: str, path: str) -> SkillReadResult:
        skill = self._skills.get(name)
        if skill is None:
            return self._unknown_skill(name)
        text = skill.package.read(path)
        if text is None:
            return SkillRefusal(
                error="unknown_file",
                message=f"{name} has no file {path!r}.",
                files=[item.path for item in skill.package.files],
            )
        return SkillResource(name=name, path=path, text=text)

    def _unknown_skill(self, name: str) -> SkillRefusal:
        return SkillRefusal(
            error="unknown_skill",
            message=f"No skill is named {name!r}.",
            names=[skill_name for skill_name, skill in self._skills.items() if skill.owner != "everyone"],
        )
