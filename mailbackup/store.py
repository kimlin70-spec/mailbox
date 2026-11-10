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


def default_base_dir() -> str:
    return os.path.join(documents_dir(), "MailBackup")


class Index:
    """이미 받은 메일 목록. 한 줄에 한 통씩 덧붙여 기록한다."""

    def __init__(self, base_dir: str):
        self._dir = os.path.join(base_dir, STATE_DIR)
        self._path = os.path.join(self._dir, INDEX_FILE)
        self._keys: set[str] = set()
        os.makedirs(self._dir, exist_ok=True)
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        self._keys.add(json.loads(line)["key"])
                    except (ValueError, KeyError, TypeError):
                        continue  # 기록 도중 끊긴 줄은 무시 (그 메일은 다시 받는다)
        self._fh = open(self._path, "a", encoding="utf-8")

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
