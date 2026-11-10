"""기능 2: 텍스트 사본.

백업해 둔 .eml 파일을 이 PC 안에서만 변환한다. (서버·외부 서비스에 연결하지 않는다.)

  텍스트\\YYYY-MM\\대화제목.md   같은 대화(스레드)를 시간순으로 한 파일에
  대화목록.csv                  전체 대화 목록

첨부 파일은 꺼내지 않는다. 파일명만 적고, 내용은 원본 메일(.eml)을 열어서 본다.
(메일에 섞여 온 위험한 파일이 PC에 바로 실행 가능한 형태로 놓이지 않게 하기 위함)
"""

from __future__ import annotations

import base64
import binascii
import csv
import email
import email.policy
import email.utils
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import cleaner, naming
from .ews import Stopped

VERSION = 3  # 본문 변환 규칙이 바뀌면 올린다 → 대화 파일 전체를 다시 만든다
CACHE_FILE = "텍스트색인_v2.jsonl"  # 메일별 색인 (형식이 바뀔 때만 이름을 바꾼다)
THREADS_FILE = "대화파일.json"
CSV_FILE = "대화목록.csv"

_RE_PREFIX = re.compile(
    r"^\s*((re|fw|fwd|tr|rv|aw|wg|réf|답장|회신|전달|응답)\s*(\[\d+\])?\s*[:：]\s*)+", re.I)
_WEEKDAYS = "월화수목금토일"


@dataclass
class TextResult:
    mails: int = 0        # 변환 대상 메일 수
    new_mails: int = 0    # 이번에 새로 읽은 메일 수
    threads: int = 0      # 전체 대화 수
    written: int = 0      # 이번에 새로 쓰거나 갱신한 대화 파일 수
    failed: int = 0       # 읽지 못한 메일 수
    csv_ok: bool = True
    no_source: bool = False  # 원본 메일이 하나도 없음


# ---------------------------------------------------------------- .eml 읽기

def _decode(part) -> str:
    try:
        return part.get_content()
    except Exception:
        raw = part.get_payload(decode=True) or b""
        for cs in (part.get_content_charset(), "utf-8", "cp949", "cp1252"):
            if not cs:
                continue
            try:
                return raw.decode(cs)
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", "replace")


def _body_text(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    text = _decode(part)
    if not isinstance(text, str):
        return ""
    if part.get_content_type() == "text/html":
        return cleaner.html_to_text(text)
    return cleaner.tidy(text)


def _header(msg, name: str) -> str:
    try:
        return re.sub(r"\s+", " ", str(msg.get(name, "") or "")).strip()
    except Exception:
        return ""


def _addresses(msg, name: str) -> list[str]:
    """['이름 <주소>', …]"""
    result = []
    try:
        values = [str(v) for v in msg.get_all(name, [])]
    except Exception:
        values = []
    for disp, addr in email.utils.getaddresses(values):
        disp = re.sub(r"\s+", " ", disp).strip().strip("'\"")
        if disp and addr and disp.lower() != addr.lower():
            result.append("%s <%s>" % (disp, addr))
        elif addr or disp:
            result.append(addr or disp)
    return result


def _short_name(full: str) -> str:
    return full.split(" <")[0] if " <" in full else full


def _msg_date(msg, rel_path: str) -> datetime | None:
    try:
        dt = email.utils.parsedate_to_datetime(_header(msg, "Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})_", os.path.basename(rel_path))
    if m:
        try:
            return datetime(*map(int, m.groups())).astimezone().astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _ids(text: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", text or "")


def _thread_index(msg) -> str:
    """Outlook/Exchange 가 붙이는 대화 번호 (앞 22바이트가 같으면 같은 대화)."""
    raw = _header(msg, "Thread-Index")
    if not raw:
        return ""
    try:
        data = base64.b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        return ""
    return data[:22].hex() if len(data) >= 22 else ""


def normalize_subject(subject: str) -> tuple[str, bool]:
    """(머리말 뗀 제목, 답장/전달 머리말이 있었는지)"""
    norm = _RE_PREFIX.sub("", subject or "").strip()
    return (norm or (subject or "").strip()), norm != (subject or "").strip()


def _is_inline_image(part, html: str) -> bool:
    """본문에 박힌 그림(서명 로고 등)인지. 확실할 때만 True."""
    if part.get_content_maintype() != "image":
        return False
    cid = (part.get("Content-ID") or "").strip().strip("<>")
    if cid and ("cid:" + cid) in html:
        return True
    return part.get_content_disposition() == "inline" and bool(cid)


def _attachment_names(msg) -> list[str]:
    """첨부 파일 이름 목록. (파일은 꺼내지 않는다.)"""
    html = ""
    try:
        hp = msg.get_body(preferencelist=("html",))
        if hp is not None:
            html = _decode(hp) or ""
    except Exception:
        pass
    names = []
    for part in msg.walk():
        is_mail = part.get_content_type() == "message/rfc822"
        if part.is_multipart() and not is_mail:
            continue
        try:
            filename = part.get_filename()
        except Exception:
            filename = None
        disp = part.get_content_disposition()
        if is_mail and not filename:
            if disp != "attachment":
                continue
            filename = "첨부메일.eml"
        if not filename and disp != "attachment":
            continue
        if _is_inline_image(part, html):
            continue
        names.append(naming.clean(filename or "이름없는첨부", 100, "이름없는첨부"))
    return names


def _read_eml(base_dir: str, rel_path: str):
    with open(os.path.join(base_dir, rel_path), "rb") as fh:
        return email.message_from_binary_file(fh, policy=email.policy.default)


def _index_eml(base_dir: str, rel_path: str) -> dict:
    msg = _read_eml(base_dir, rel_path)
    date = _msg_date(msg, rel_path)
    subject = _header(msg, "Subject")
    norm, prefixed = normalize_subject(subject)
    mids = _ids(_header(msg, "Message-ID"))
    senders = _addresses(msg, "From")
    return {
        "path": rel_path,
        "key": mids[0] if mids else "path:" + rel_path,
        "refs": sorted(set(_ids(_header(msg, "In-Reply-To")) + _ids(_header(msg, "References")))),
        "ti": _thread_index(msg),
        "subject": subject,
        "norm": norm,
        "prefixed": prefixed,
        "date": date.strftime("%Y-%m-%dT%H:%M:%SZ") if date else "",
        "from": senders[0] if senders else "",
        "to": _addresses(msg, "To"),
        "cc": _addresses(msg, "Cc"),
        "atts": _attachment_names(msg),
    }


# ---------------------------------------------------------------- 대화 묶기

class _Union:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def group_threads(entries: list[dict]) -> list[list[dict]]:
    """메일들을 대화별로 묶는다. 각 대화는 시간순."""
    uf = _Union()
    for e in entries:
        node = "p:" + e["path"]
        uf.union(node, "m:" + e["key"])
        for ref in e["refs"]:
            uf.union(node, "m:" + ref)
        if e["ti"]:
            uf.union(node, "t:" + e["ti"])

    # 연결 정보가 전혀 없는 답장/전달 메일은 제목으로 묶는다.
    # 단, 같은 제목의 원래 대화가 하나뿐일 때만 (여러 개면 애매하므로 따로 둔다).
    orphans: dict[str, list[dict]] = {}
    originals: dict[str, set[str]] = {}
    for e in entries:
        subj = e["norm"].lower()
        if not subj:
            continue
        if e["prefixed"] and not e["refs"] and not e["ti"]:
            orphans.setdefault(subj, []).append(e)
        elif not e["prefixed"]:
            originals.setdefault(subj, set()).add(uf.find("p:" + e["path"]))
    for subj, group in orphans.items():
        for e in group[1:]:
            uf.union("p:" + group[0]["path"], "p:" + e["path"])
        roots = originals.get(subj, set())
        if len(roots) == 1:
            uf.union("p:" + group[0]["path"], next(iter(roots)))

    buckets: dict[str, list[dict]] = {}
    for e in entries:
        buckets.setdefault(uf.find("p:" + e["path"]), []).append(e)
    threads = []
    for members in buckets.values():
        members.sort(key=lambda e: (e["date"] or "9999", e["path"]))
        seen, unique = set(), []
        for e in members:  # 받은편지함·보낸편지함 양쪽에 있는 같은 메일은 한 번만
            if e["key"] not in seen:
                seen.add(e["key"])
                unique.append(e)
        threads.append(unique)
    threads.sort(key=lambda t: (t[-1]["date"] or "9999", t[0]["path"]))
    return threads


# ---------------------------------------------------------------- 쓰기

def _local(iso: str) -> datetime | None:
    if not iso:
        return None
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()


def _fmt(iso: str) -> str:
    dt = _local(iso)
    return "%s (%s)" % (dt.strftime("%Y-%m-%d %H:%M"), _WEEKDAYS[dt.weekday()]) if dt else "날짜 모름"


def thread_title(thread: list[dict]) -> str:
    return thread[0]["norm"] or "제목없음"


def participants(thread: list[dict]) -> list[str]:
    names: dict[str, None] = {}
    for e in thread:
        for full in [e["from"]] + e["to"] + e["cc"]:
            if full:
                names.setdefault(_short_name(full))
    return list(names)


def render_thread(base_dir: str, thread: list[dict]) -> str:
    title = thread_title(thread)
    people = participants(thread)
    out = ["# %s" % title, "",
           "- 메일 수: %d" % len(thread),
           "- 기간: %s ~ %s" % (_fmt(thread[0]["date"]), _fmt(thread[-1]["date"])),
           "- 참여자: %s" % ", ".join(people), ""]
    earlier: list[str] = []
    for e in thread:
        try:
            raw = _body_text(_read_eml(base_dir, e["path"]))
        except Exception:
            raw = ""
        body = cleaner.clean_body(raw, earlier) if raw else ""
        earlier.append(cleaner.squash(raw))
        out += ["---", "", "## %s · %s" % (_fmt(e["date"]), _short_name(e["from"]) or "보낸사람 모름"), "",
                "- 날짜: %s" % _fmt(e["date"]),
                "- 보낸사람: %s" % (e["from"] or "-"),
                "- 받는사람: %s" % ("; ".join(e["to"]) or "-"),
                "- 참조: %s" % ("; ".join(e["cc"]) or "-"),
                "- 제목: %s" % (e["subject"] or "(제목 없음)")]
        if e["atts"]:
            out.append("- 첨부: %s (원본 메일을 열어서 확인)" % "; ".join(e["atts"]))
        out.append("- 원본 메일: %s" % e["path"])
        out += ["", body or "(본문 없음)", ""]
    return "\n".join(out).rstrip() + "\n"


def _write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding=encoding, newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _remove_old_version_output(base_dir: str, state_dir: str, log) -> None:
    """예전 버전이 만든 결과물 정리: 꺼내 놓았던 첨부 파일과 예전 규칙의 대화 파일.

    이 프로그램이 만들었다고 기록해 둔 파일만 지운다. 원본 .eml 은 건드리지 않는다.
    """
    removed = 0
    old_cache = os.path.join(state_dir, "텍스트색인_v1.jsonl")
    if os.path.exists(old_cache):
        att_root = os.path.join(base_dir, "첨부")
        with open(old_cache, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    atts = json.loads(line).get("atts", [])
                except (ValueError, AttributeError):
                    continue
                for att in atts:
                    if not (isinstance(att, list) and len(att) == 2 and isinstance(att[1], str)):
                        continue
                    path = os.path.normpath(os.path.join(base_dir, att[1]))
                    if not path.startswith(att_root + os.sep):
                        continue
                    try:
                        os.remove(path)
                        removed += 1
                    except OSError:
                        pass
        if os.path.isdir(att_root):
            for name in os.listdir(att_root):
                try:
                    os.rmdir(os.path.join(att_root, name))  # 빈 폴더만 지워진다
                except OSError:
                    pass
            try:
                os.rmdir(att_root)
            except OSError:
                pass
        try:
            os.remove(old_cache)
        except OSError:
            pass
    threads_path = os.path.join(state_dir, THREADS_FILE)
    state = _load_json(threads_path, {})
    if isinstance(state, dict) and state.get("version") not in (None, VERSION):
        text_root = os.path.join(base_dir, "텍스트")
        for rel in state.get("files", {}):
            path = os.path.normpath(os.path.join(base_dir, rel))
            if path.startswith(text_root + os.sep) and path.endswith(".md"):
                try:
                    os.remove(path)
                except OSError:
                    pass
        try:
            os.remove(threads_path)
        except OSError:
            pass
    if removed:
        log.info("예전 버전이 꺼내 둔 첨부 파일 %d개 정리", removed)


def run_textcopy(base_dir: str, on_progress=None, stop_event=None,
                 logger: logging.Logger | None = None) -> TextResult:
    log = logger or logging.getLogger("mailbackup")
    res = TextResult()
    state_dir = os.path.join(base_dir, "상태")
    os.makedirs(state_dir, exist_ok=True)

    def report(stage: str, done: int, total: int):
        if on_progress:
            on_progress(stage, done, total)

    def check_stop():
        if stop_event is not None and stop_event.is_set():
            raise Stopped()

    _remove_old_version_output(base_dir, state_dir, log)

    # 1) 새로 받은 .eml 읽기 (한 번 읽은 메일은 색인에 적어 두고 다시 읽지 않는다)
    cache_path = os.path.join(state_dir, CACHE_FILE)
    cache: dict[str, dict] = {}
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                    cache[e["path"]] = e
                except (ValueError, KeyError, TypeError):
                    continue

    eml_paths = []
    src = os.path.join(base_dir, "원본")
    for root, _dirs, files in os.walk(src):
        for name in files:
            if name.lower().endswith(".eml"):
                eml_paths.append(os.path.relpath(os.path.join(root, name), base_dir))
    eml_paths.sort()
    todo = [p for p in eml_paths if p not in cache]
    res.mails = len(eml_paths)
    if not eml_paths:
        # 원본 폴더가 비어 있으면(폴더를 옮기는 중 등) 기존 텍스트 사본을 지우지 않고 그대로 둔다.
        res.no_source = True
        log.info("텍스트 사본: 원본 메일이 없어 아무것도 바꾸지 않음")
        return res

    with open(cache_path, "a", encoding="utf-8") as fh:
        for i, rel in enumerate(todo):
            check_stop()
            if i % 20 == 0:
                report("메일을 읽는 중…", i, len(todo))
            try:
                entry = _index_eml(base_dir, rel)
            except Exception as exc:
                res.failed += 1
                log.info("메일 읽기 실패: %s", type(exc).__name__)
                continue
            cache[rel] = entry
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            res.new_mails += 1
    present = set(eml_paths)
    entries = [e for p, e in cache.items() if p in present]

    # 2) 대화 묶기
    report("대화를 묶는 중…", 0, 0)
    threads = group_threads(entries)
    res.threads = len(threads)

    # 3) 바뀐 대화만 다시 쓰기
    threads_path = os.path.join(state_dir, THREADS_FILE)
    state = _load_json(threads_path, {})
    old_files: dict[str, list[str]] = state.get("files", {}) if state.get("version") == VERSION else {}
    key_to_file = {k: f for f, keys in old_files.items() for k in keys}
    new_files: dict[str, list[str]] = {}
    rows = []
    claimed: set[str] = set()  # 이번 실행에서 이미 다른 대화가 쓰고 있는 파일

    def save_state():
        merged = dict(old_files)
        merged.update(new_files)
        _write_text(threads_path, json.dumps({"version": VERSION, "files": merged}, ensure_ascii=False))

    try:
        for i, thread in enumerate(threads):
            check_stop()
            if i % 20 == 0:
                report("대화 파일을 만드는 중…", i, len(threads))
            keys = [e["key"] for e in thread]
            previous = {key_to_file[k] for k in keys if k in key_to_file}
            last = _local(thread[-1]["date"])
            rel_dir = os.path.join("텍스트", last.strftime("%Y-%m") if last else "날짜없음")
            stem = naming.clean(thread_title(thread), 80, "제목없음")

            rel = None
            if len(previous) == 1 and next(iter(previous)) not in claimed:
                old = next(iter(previous))
                same_place = os.path.dirname(old) == rel_dir and re.fullmatch(
                    re.escape(stem) + r"(_\d+)?\.md", os.path.basename(old))
                if same_place and os.path.exists(os.path.join(base_dir, old)):
                    if set(old_files.get(old, [])) == set(keys):
                        new_files[old] = keys  # 바뀐 것 없음
                        claimed.add(old)
                        rows.append((thread, old))
                        continue
                    rel = old  # 같은 파일을 갱신

            for old in previous:
                if old != rel and old not in claimed:
                    try:
                        os.remove(os.path.join(base_dir, old))
                    except OSError:
                        pass
                old_files.pop(old, None)
            if rel is None:
                os.makedirs(os.path.join(base_dir, rel_dir), exist_ok=True)
                rel = os.path.relpath(
                    naming.unique_path(os.path.join(base_dir, rel_dir), stem, ".md"), base_dir)
            try:
                _write_text(os.path.join(base_dir, rel), render_thread(base_dir, thread))
            except OSError as exc:
                if getattr(exc, "errno", None) == 28:  # 디스크 공간 부족
                    raise
                res.failed += 1  # 이 대화만 건너뛰고 다음 실행 때 다시 시도
                log.info("대화 파일 쓰기 실패: %s", type(exc).__name__)
                continue
            new_files[rel] = keys
            claimed.add(rel)
            rows.append((thread, rel))
            res.written += 1
            if res.written % 200 == 0:
                save_state()
    finally:
        save_state()

    # 더 이상 해당하는 대화가 없는 예전 파일 정리 (원본 .eml 을 지운 경우 등)
    for old in list(old_files):
        if old not in new_files:
            try:
                os.remove(os.path.join(base_dir, old))
            except OSError:
                pass
            old_files.pop(old, None)
    save_state()
    text_root = os.path.join(base_dir, "텍스트")
    if os.path.isdir(text_root):
        for name in os.listdir(text_root):
            try:
                os.rmdir(os.path.join(text_root, name))  # 빈 월 폴더만 지워진다
            except OSError:
                pass

    # 4) 전체 대화 목록 CSV (엑셀에서 한글이 깨지지 않게 UTF-8 BOM)
    report("대화 목록을 만드는 중…", 0, 0)
    rows.sort(key=lambda r: r[0][-1]["date"] or "", reverse=True)
    csv_path = os.path.join(base_dir, CSV_FILE)
    try:
        tmp = csv_path + ".part"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["마지막 날짜", "제목", "참여자", "메일 수", "파일위치"])
            for thread, rel in rows:
                last = _local(thread[-1]["date"])
                w.writerow([last.strftime("%Y-%m-%d %H:%M") if last else "", thread_title(thread),
                            ", ".join(participants(thread)), len(thread), rel])
        os.replace(tmp, csv_path)
    except OSError:
        res.csv_ok = False  # 엑셀에서 열어 둔 경우 등

    log.info("텍스트 사본: 메일 %d통(새로 읽음 %d, 실패 %d), 대화 %d개(갱신 %d)%s",
             res.mails, res.new_mails, res.failed, res.threads, res.written,
             "" if res.csv_ok else ", 대화목록 저장 실패")
    return res
