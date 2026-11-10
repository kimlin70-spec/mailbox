"""PC 쪽 저장: 저장 위치, 받은 메일 목록(중복 방지), .eml 쓰기."""

from __future__ import annotations

import json
import os
import sys

from . import naming

STATE_DIR = "상태"
INDEX_FILE = "받은목록.jsonl"


def documents_dir() -> str:
    """'내 문서' 폴더 위치. (OneDrive 등으로 옮겨져 있어도 실제 위치를 찾는다.)"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5 (내 문서)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def desktop_dir() -> str:
    """바탕화면 폴더의 실제 위치."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_DESKTOPDIRECTORY = 0x10
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def default_base_dir() -> str:
    return os.path.join(desktop_dir(), "MailBackup")


def old_default_base_dir() -> str:
    """0.2 버전까지의 기본 위치 (내 문서\\MailBackup)."""
    return os.path.join(documents_dir(), "MailBackup")


def cloud_sync_name(path: str) -> str:
    """경로가 클라우드와 자동 동기화되는 폴더 안이면 그 서비스 이름, 아니면 빈 문자열."""
    norm = os.path.normcase(os.path.abspath(path))
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root and (norm + os.sep).startswith(os.path.normcase(os.path.abspath(root)) + os.sep):
            return "OneDrive"
    parts = [p.lower() for p in norm.replace("\\", "/").split("/")]
    for part in parts:
        if part.startswith("onedrive"):
            return "OneDrive"
        if part in ("dropbox", "google drive", "googledrive", "my drive", "내 드라이브", "icloud drive",
                    "iclouddrive"):
            return "클라우드"
    return ""


def has_saved_base_dir() -> bool:
    return os.path.exists(_settings_path())


def acquire_lock(base_dir: str):
    """같은 저장 폴더로 두 번 동시에 실행되는 것을 막는다. 이미 실행 중이면 None."""
    state = os.path.join(base_dir, STATE_DIR)
    os.makedirs(state, exist_ok=True)
    fh = open(os.path.join(state, "실행중.lock"), "a+")
    try:
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _settings_path() -> str:
    root = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(root, "MailBackup", "settings.json")


def load_base_dir() -> str:
    """저장 위치. 따로 정한 적이 없으면 바탕화면\\MailBackup."""
    try:
        with open(_settings_path(), "r", encoding="utf-8") as fh:
            value = json.load(fh).get("base_dir")
        if isinstance(value, str) and value:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return default_base_dir()


def save_base_dir(base_dir: str) -> None:
    """저장 위치만 기억한다. (아이디·비밀번호는 저장하지 않는다.)"""
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"base_dir": base_dir}, fh, ensure_ascii=False)


class Index:
    """이미 받은 메일 목록. 한 줄에 한 통씩 덧붙여 기록한다."""

    def __init__(self, base_dir: str):
        self._dir = os.path.join(base_dir, STATE_DIR)
        self._path = os.path.join(self._dir, INDEX_FILE)
        self._keys: set[str] = set()
        os.makedirs(self._dir, exist_ok=True)
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        self._keys.add(json.loads(line)["key"])
                    except (ValueError, KeyError, TypeError):
                        continue  # 기록 도중 끊긴 줄은 무시 (그 메일은 다시 받는다)
        self._fh = open(self._path, "a", encoding="utf-8")
        if self._fh.tell() > 0:  # 지난번에 줄 중간에서 끊겼어도 새 기록이 붙어 버리지 않게
            self._fh.write("\n")

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key: str, rel_path: str) -> None:
        self._fh.write(json.dumps({"key": key, "path": rel_path}, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._keys.add(key)

    def close(self) -> None:
        self._fh.close()


def item_key(root_name: str, message_id: str, item_id: str) -> str:
    """같은 메일인지 판단하는 값.

    메일 고유 번호(Message-ID)를 쓰므로 서버에서 폴더를 옮겨도 다시 받지 않는다.
    받은편지함과 보낸편지함은 따로 센다 (나에게 보낸 메일은 양쪽에 모두 저장).
    """
    if message_id:
        return "%s|mid|%s" % (root_name, message_id)
    return "%s|id|%s" % (root_name, item_id)


def save_eml(base_dir: str, folder_parts: list[str], received, sender: str, subject: str,
             content: bytes) -> str:
    """원본\\폴더명\\YYYY-MM\\날짜_시분_보낸사람_제목.eml 로 저장하고 상대 경로를 돌려준다."""
    rel_dir = os.path.join("원본", *folder_parts, naming.month_folder(received))
    abs_dir = os.path.join(base_dir, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    path = naming.unique_path(abs_dir, naming.eml_name(received, sender, subject), ".eml")
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return os.path.relpath(path, base_dir)
