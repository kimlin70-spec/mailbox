"""기능 1: 원본 백업 (받은편지함+하위폴더, 보낸편지함 → .eml)."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import ews, naming, store

PAGE_SIZE = 50            # 목록은 한 번에 50통씩 확인
BATCH_COUNT = 10          # 원본은 한 번에 최대 10통씩
BATCH_BYTES = 15 * 1024 * 1024  # 또는 합계 약 15MB까지

MIN_FREE_BYTES = 500 * 1024 * 1024  # 디스크에 최소 이만큼은 남긴다


class LowDisk(Exception):
    """저장할 디스크의 여유 공간이 부족함."""


def check_disk(base_dir: str, need: int = 0) -> None:
    try:
        free = shutil.disk_usage(base_dir).free
    except OSError:
        return
    if free < MIN_FREE_BYTES + need:
        raise LowDisk()


def _get_mime_safely(client, ids: list[str]) -> list[ews.MimeResult]:
    """묶음 전체가 거절되면 한 통씩 다시 받아, 문제 있는 메일만 건너뛴다."""
    try:
        return client.get_mime(ids)
    except ews.EwsError as exc:
        if exc.code == "ErrorServerBusy" or len(ids) == 1:
            if len(ids) == 1 and exc.code != "ErrorServerBusy":
                return [ews.MimeResult(ids[0], None, exc.code)]
            raise
    results = []
    for item_id in ids:
        try:
            results.extend(client.get_mime([item_id]))
        except ews.EwsError as exc:
            if exc.code == "ErrorServerBusy":
                raise
            results.append(ews.MimeResult(item_id, None, exc.code))
    return results


ROOTS = (
    ("받은편지함", ews.INBOX),
    ("보낸편지함", ews.SENT),
)


@dataclass
class Progress:
    stage: str = ""          # 화면에 보여 줄 현재 단계
    folder: str = ""
    total: int = 0           # 확인해야 할 전체 메일 수
    checked: int = 0         # 지금까지 확인한 수
    saved: int = 0           # 새로 저장한 수
    skipped: int = 0         # 이미 받아서 건너뛴 수
    failed: int = 0          # 받지 못한 수


@dataclass
class _Folder:
    ref: ews.FolderRef
    parts: list[str]
    root_name: str
    total: int = 0
    display: str = field(init=False)

    def __post_init__(self):
        self.display = " > ".join(self.parts)


def setup_logging(base_dir: str) -> logging.Logger:
    """로그에는 시각·폴더 이름·건수·오류 이름만 남긴다. 비밀번호·메일 내용은 남기지 않는다."""
    log_dir = os.path.join(base_dir, "로그")
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("mailbackup")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    handler = logging.FileHandler(
        os.path.join(log_dir, "backup_%s.log" % datetime.now().strftime("%Y%m%d")), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


def _list_folders(client: ews.EwsClient) -> list[_Folder]:
    result: list[_Folder] = []
    for root_name, root_ref in ROOTS:
        result.append(_Folder(root_ref, [root_name], root_name))
        infos = client.find_folders(root_ref)
        by_id = {f.folder_id: f for f in infos}
        for info in infos:
            # 메일 폴더만 (일정·연락처 폴더 등은 제외)
            if info.folder_class and not info.folder_class.startswith("IPF.Note"):
                continue
            names = []
            cur, guard = info, 0
            while cur is not None and guard < 50:
                names.append(naming.clean(cur.name, 50, "이름없는폴더"))
                cur = by_id.get(cur.parent_id)
                guard += 1
            parts = [root_name] + names[::-1]
            result.append(_Folder(ews.FolderRef("id", info.folder_id), parts, root_name))
    return result


def run_backup(client: ews.EwsClient, base_dir: str, days: int | None = None,
               on_progress=None, stop_event=None, logger: logging.Logger | None = None) -> Progress:
    """백업을 실행한다. 중간에 멈춰도 다시 실행하면 이어서 받는다."""
    log = logger or logging.getLogger("mailbackup")
    prog = Progress()

    def report(stage: str | None = None):
        if stage is not None:
            prog.stage = stage
        if on_progress:
            on_progress(prog)

    def check_stop():
        if stop_event is not None and stop_event.is_set():
            raise ews.Stopped()

    since = None
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0)

    os.makedirs(base_dir, exist_ok=True)
    check_disk(base_dir)
    index = store.Index(base_dir)
    try:
        report("서버에 연결하는 중…")
        client.connect()
        log.info("연결됨 (로그인 방식 %s, 범위 %s)", client.auth_scheme,
                 "최근 %d일" % days if days else "전체")

        report("폴더 목록을 확인하는 중…")
        folders = _list_folders(client)
        log.info("폴더 %d개", len(folders))

        report("메일 수를 세는 중…")
        for f in folders:
            check_stop()
            _, f.total, _ = client.find_items(f.ref, 0, 1, since)
            prog.total += f.total
            report()
        log.info("확인할 메일 %d통", prog.total)

        for f in folders:
            prog.folder = f.display
            report("메일을 받는 중…")
            before = (prog.saved, prog.skipped, prog.failed)
            _backup_folder(client, index, base_dir, f, since, prog, report, check_stop, log)
            log.info("폴더 [%s] 새로 저장 %d, 건너뜀 %d, 실패 %d", f.display,
                     prog.saved - before[0], prog.skipped - before[1], prog.failed - before[2])

        report("완료")
        log.info("완료: 새로 저장 %d, 건너뜀 %d, 실패 %d", prog.saved, prog.skipped, prog.failed)
        return prog
    except ews.Stopped:
        log.info("사용자가 중지: 새로 저장 %d, 건너뜀 %d, 실패 %d",
                 prog.saved, prog.skipped, prog.failed)
        raise
    except Exception as exc:
        code = getattr(exc, "code", "")
        log.info("오류로 중단: %s %s", type(exc).__name__, code)
        raise
    finally:
        index.close()


def _backup_folder(client, index, base_dir, f: _Folder, since, prog: Progress,
                   report, check_stop, log) -> None:
    offset = 0
    while True:
        check_stop()
        items, _, last = client.find_items(f.ref, offset, PAGE_SIZE, since)
        offset += len(items)

        todo = []
        for it in items:
            key = store.item_key(f.root_name, it.message_id, it.item_id)
            if key in index:
                prog.skipped += 1
                prog.checked += 1
            else:
                todo.append((key, it))
        report()

        while todo:
            check_stop()
            batch, size = [], 0
            while todo and len(batch) < BATCH_COUNT and (not batch or size + todo[0][1].size <= BATCH_BYTES):
                size += todo[0][1].size
                batch.append(todo.pop(0))
            check_disk(base_dir, size * 2)
            results = _get_mime_safely(client, [it.item_id for _, it in batch])
            for (key, it), res in zip(batch, results):
                prog.checked += 1
                if key in index:  # 같은 묶음 안에 같은 메일이 두 번 있는 경우
                    prog.skipped += 1
                    continue
                if res.content is None:
                    prog.failed += 1
                    log.info("받지 못함: 폴더 [%s] 날짜 %s 오류 %s", f.display,
                             it.received.astimezone().strftime("%Y-%m-%d %H:%M") if it.received else "-",
                             res.error)
                    continue
                try:
                    rel = store.save_eml(base_dir, f.parts, it.received, it.sender, it.subject,
                                         res.content)
                except OSError as exc:
                    prog.failed += 1
                    log.info("저장 실패: 폴더 [%s] 오류 %s", f.display, type(exc).__name__)
                    if getattr(exc, "errno", None) == 28:  # 디스크 공간 부족
                        raise
                    continue
                index.add(key, rel)
                prog.saved += 1
            report()

        if last or not items:
            return
