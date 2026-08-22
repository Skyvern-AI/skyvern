from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _TabRecord:
    target_id: str
    root_session_id: str


@dataclass(frozen=True, slots=True)
class _ChildSessionRecord:
    tab_id: int
    target_id: str


class VirtualTargetRegistry:
    def __init__(self) -> None:
        self._attach_counter = 0
        self._alias_counter = 0
        self._tabs: dict[int, _TabRecord] = {}
        self._root_sessions: dict[str, int] = {}
        self._tab_root_aliases: dict[int, list[str]] = {}
        self._browser_session_aliases: set[str] = set()
        self._child_sessions: dict[str, _ChildSessionRecord] = {}
        self._child_target_sessions: dict[str, str] = {}
        self._target_tabs: dict[str, int] = {}
        self._target_infos: dict[str, dict] = {}

    def register_tab(self, tab_id: int, url: str, title: str, target_id: str | None = None) -> str:
        self.remove_tab(tab_id)
        self._attach_counter += 1
        # Playwright requires the page targetId to equal Chrome's main-frame id;
        # callers pass the real frame id once the debugger is attached.
        target_id = target_id or f"tab-{tab_id}"
        root_session_id = f"sess-tab-{tab_id}-{self._attach_counter}"
        self._tabs[tab_id] = _TabRecord(target_id=target_id, root_session_id=root_session_id)
        self._root_sessions[root_session_id] = tab_id
        self._target_tabs[target_id] = tab_id
        self._target_infos[target_id] = {
            "targetId": target_id,
            "type": "page",
            "title": title,
            "url": url,
            "attached": True,
            "canAccessOpener": False,
            "browserContextId": "skyvern-default",
        }
        return target_id

    def remove_tab(self, tab_id: int) -> None:
        tab = self._tabs.pop(tab_id, None)
        if tab is not None:
            self._root_sessions.pop(tab.root_session_id, None)
            for alias_session_id in self._tab_root_aliases.pop(tab_id, []):
                self._root_sessions.pop(alias_session_id, None)
            self._target_tabs.pop(tab.target_id, None)
            self._target_infos.pop(tab.target_id, None)

        child_session_ids = [
            session_id for session_id, record in self._child_sessions.items() if record.tab_id == tab_id
        ]
        for session_id in child_session_ids:
            self.remove_child_session(session_id)

    def root_session_id(self, tab_id: int) -> str:
        return self._tabs[tab_id].root_session_id

    def root_session_ids(self, tab_id: int) -> list[str]:
        return [self.root_session_id(tab_id), *self._tab_root_aliases.get(tab_id, [])]

    def create_root_session_alias(self, tab_id: int) -> str:
        if tab_id not in self._tabs:
            raise KeyError(tab_id)
        self._alias_counter += 1
        session_id = f"alias-tab-{tab_id}-{self._alias_counter}"
        self._root_sessions[session_id] = tab_id
        self._tab_root_aliases.setdefault(tab_id, []).append(session_id)
        return session_id

    def remove_root_session_alias(self, session_id: str) -> bool:
        tab_id = self._root_sessions.get(session_id)
        if tab_id is None:
            return False
        aliases = self._tab_root_aliases.get(tab_id)
        if aliases is None or session_id not in aliases:
            return False
        aliases.remove(session_id)
        if not aliases:
            self._tab_root_aliases.pop(tab_id, None)
        self._root_sessions.pop(session_id, None)
        return True

    def create_browser_session_alias(self) -> str:
        self._alias_counter += 1
        session_id = f"browser-alias-{self._alias_counter}"
        self._browser_session_aliases.add(session_id)
        return session_id

    def is_browser_session_alias(self, session_id: str) -> bool:
        return session_id in self._browser_session_aliases

    def remove_browser_session_alias(self, session_id: str) -> bool:
        if session_id not in self._browser_session_aliases:
            return False
        self._browser_session_aliases.remove(session_id)
        return True

    def has_tab(self, tab_id: int) -> bool:
        return tab_id in self._tabs

    def target_id_for_tab(self, tab_id: int) -> str:
        return self._tabs[tab_id].target_id

    def target_info_for_tab(self, tab_id: int) -> dict:
        return self.target_info(self._tabs[tab_id].target_id)

    def register_child_session(self, tab_id: int, chrome_session_id: str, target_info: dict) -> None:
        if tab_id not in self._tabs:
            raise KeyError(tab_id)
        target_id = target_info["targetId"]
        if not isinstance(target_id, str):
            raise KeyError("targetId")

        self.remove_child_session(chrome_session_id)
        existing_session_id = self._child_target_sessions.get(target_id)
        if existing_session_id is not None:
            self.remove_child_session(existing_session_id)

        self._child_sessions[chrome_session_id] = _ChildSessionRecord(tab_id=tab_id, target_id=target_id)
        self._child_target_sessions[target_id] = chrome_session_id
        self._target_tabs[target_id] = tab_id
        self._target_infos[target_id] = dict(target_info)

    def remove_child_session(self, chrome_session_id: str) -> None:
        record = self._child_sessions.pop(chrome_session_id, None)
        if record is None:
            return
        if self._child_target_sessions.get(record.target_id) == chrome_session_id:
            self._child_target_sessions.pop(record.target_id, None)
            self._target_tabs.pop(record.target_id, None)
            self._target_infos.pop(record.target_id, None)

    def resolve_session(self, session_id: str) -> tuple[int, str | None]:
        root_tab_id = self._root_sessions.get(session_id)
        if root_tab_id is not None:
            return root_tab_id, None
        child = self._child_sessions.get(session_id)
        if child is None:
            raise KeyError(session_id)
        return child.tab_id, session_id

    def target_info(self, target_id: str) -> dict:
        return dict(self._target_infos[target_id])

    def list_page_targets(self) -> list[dict]:
        return [self.target_info(tab.target_id) for tab in self._tabs.values()]

    def tab_for_target(self, target_id: str) -> int:
        return self._target_tabs[target_id]

    def update_tab(self, tab_id: int, url: str, title: str) -> None:
        target_id = self._tabs[tab_id].target_id
        self._target_infos[target_id]["url"] = url
        self._target_infos[target_id]["title"] = title

    def clear(self) -> None:
        self._tabs.clear()
        self._root_sessions.clear()
        self._tab_root_aliases.clear()
        self._browser_session_aliases.clear()
        self._child_sessions.clear()
        self._child_target_sessions.clear()
        self._target_tabs.clear()
        self._target_infos.clear()
