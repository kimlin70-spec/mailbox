"""Windows에서 안전한 파일·폴더 이름 만들기."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {
    "LPT%d" % i for i in range(1, 10)}


def clean(text: str, max_len: int, fallback: str) -> str:
    """파일 이름의 한 부분으로 쓸 수 있게 다듬는다."""
    text = unicodedata.normalize("NFC", text or "")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Cs", "Co", "Cn"))
    text = _FORBIDDEN.sub(" ", text)
    text = _SPACES.sub(" ", text).strip(" ._")
    if len(text) > max_len:
        text = text[:max_len].rstrip(" ._")
    if not text:
        text = fallback
    if text.split(".")[0].upper() in _RESERVED:
        text = "_" + text
    return text


def month_folder(received: datetime | None) -> str:
    return received.astimezone().strftime("%Y-%m") if received else "날짜없음"


def eml_name(received: datetime | None, sender: str, subject: str) -> str:
    """날짜_시분_보낸사람_제목 (확장자 제외). 시각은 이 PC의 현지 시간."""
    stamp = received.astimezone().strftime("%Y-%m-%d_%H%M") if received else "날짜없음"
    return "%s_%s_%s" % (stamp, clean(sender, 30, "보낸사람없음"), clean(subject, 80, "제목없음"))


def unique_path(directory: str, stem: str, ext: str) -> str:
    """같은 이름이 이미 있으면 _2, _3 … 을 붙인다."""
    path = os.path.join(directory, stem + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(directory, "%s_%d%s" % (stem, n, ext))
        n += 1
    return path
