import glob
import os
import re
import threading

import pytest

from mailbackup import backup, ews, naming, store
from tests import fake_ews


@pytest.fixture
def server():
    mb = fake_ews.FakeMailbox()
    srv, url = fake_ews.start(mb)
    yield mb, url
    srv.shutdown()


def client(url, user="TOAMASINA\\kim", password="pw", **kw):
    return ews.EwsClient(user, password, url=url, pause=0, **kw)


def fill(mb):
    mb.add("inbox", "I1", "RE: 회의 일정?", "홍길동", "2026-01-05T08:00:00Z", "<1@x>")
    mb.add("inbox", "I2", "Réunion: budget <final>", "Élodie Dupont", "2026-02-10T09:30:00Z", "<2@x>")
    mb.add("F-proj", "I3", "Shutdown plan", "John", "2026-02-11T10:00:00Z", "<3@x>")
    mb.add("F-sub", "I4", "Compte rendu", "Marie", "2026-03-01T10:00:00Z", "<4@x>")
    mb.add("F-cal", "I5", "달력 항목", "x", "2026-03-01T10:00:00Z", "<5@x>")
    mb.add("sentitems", "S1", "보고 드립니다", "나", "2026-03-02T10:00:00Z", "<6@x>")


def emls(base):
    return sorted(os.path.relpath(p, base).replace(os.sep, "/")
                  for p in glob.glob(os.path.join(base, "원본", "**", "*.eml"), recursive=True))


def test_full_backup_then_only_new(server, tmp_path):
    mb, url = server
    fill(mb)
    base = str(tmp_path)

    p = backup.run_backup(client(url), base)
    assert (p.saved, p.skipped, p.failed, p.total) == (5, 0, 0, 5)
    files = emls(base)
    assert len(files) == 5
    assert any(f.startswith("원본/받은편지함/2026-01/") and "홍길동_RE 회의 일정.eml" in f for f in files)
    assert any("원본/받은편지함/프로젝트 A B/Réunions/2026-03/" in f for f in files)
    assert any(f.startswith("원본/보낸편지함/2026-03/") for f in files)
    assert not any("일정/" in f for f in files)  # 일정 폴더는 받지 않음
    # 원본 그대로 저장
    path = glob.glob(os.path.join(base, "원본", "받은편지함", "2026-01", "*.eml"))[0]
    assert open(path, "rb").read() == mb.items["inbox"][0]["mime"]

    # 다시 실행: 새 메일 0통
    p = backup.run_backup(client(url), base)
    assert (p.saved, p.skipped) == (0, 5)

    # 새 메일 1통 + 서버에서 폴더를 옮긴 메일 → 새 메일만 받음
    mb.add("inbox", "I9", "새 메일", "홍길동", "2026-04-01T00:00:00Z", "<9@x>")
    moved = mb.items["inbox"].pop(0)
    moved["id"] = "I1-moved"
    mb.items["F-proj"].append(moved)
    p = backup.run_backup(client(url), base)
    assert (p.saved, p.skipped) == (1, 5)
    assert len(emls(base)) == 6


def test_only_read_operations_are_sent(server, tmp_path):
    mb, url = server
    fill(mb)
    backup.run_backup(client(url), str(tmp_path))
    assert set(mb.operations) <= set(ews.ALLOWED_OPERATIONS)
    with pytest.raises(RuntimeError):
        client(url)._call("DeleteItem", "<m:DeleteItem/>")


def test_source_has_no_write_operations():
    """프로그램 코드 어디에도 서버 쓰기 동작 이름이 없어야 한다."""
    forbidden = re.compile(
        r"DeleteItem|MoveItem|CopyItem|UpdateItem|CreateItem|SendItem|MarkAsJunk|MarkAllItemsAsRead|"
        r"DeleteFolder|MoveFolder|CreateFolder|UpdateFolder|EmptyFolder|CreateAttachment|DeleteAttachment|"
        r"IsRead|verify\s*=\s*False")
    pkg = os.path.dirname(backup.__file__)
    for name in os.listdir(pkg):
        if name.endswith(".py"):
            text = open(os.path.join(pkg, name), encoding="utf-8").read()
            assert not forbidden.search(text), name


def test_recent_days_only(server, tmp_path):
    from datetime import datetime, timedelta, timezone
    mb, url = server
    now = datetime.now(timezone.utc)
    mb.add("inbox", "old", "옛날", "a", (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "<o@x>")
    mb.add("inbox", "new", "최근", "a", (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), "<n@x>")
    p = backup.run_backup(client(url), str(tmp_path), days=7)
    assert (p.saved, p.total) == (1, 1)


def test_small_batches_and_paging(server, tmp_path):
    mb, url = server
    for i in range(123):
        mb.add("inbox", "M%d" % i, "제목 %d" % i, "보낸이", "2026-01-01T00:%02d:%02dZ" % (i // 60, i % 60),
               "<m%d@x>" % i)
    p = backup.run_backup(client(url), str(tmp_path))
    assert p.saved == 123 and p.checked == 123
    assert max(mb.get_item_calls) <= backup.BATCH_COUNT
    assert len(emls(str(tmp_path))) == 123


def test_same_name_gets_number_and_failed_item_retried(server, tmp_path):
    mb, url = server
    mb.add("inbox", "A", "같은 제목", "홍길동", "2026-01-05T08:00:00Z", "<a@x>")
    mb.add("inbox", "B", "같은 제목", "홍길동", "2026-01-05T08:00:30Z", "<b@x>")
    mb.add("inbox", "C", "깨진 메일", "홍길동", "2026-01-06T08:00:00Z", "<c@x>", error="ErrorItemCorrupt")
    base = str(tmp_path)
    logger = backup.setup_logging(base)
    p = backup.run_backup(client(url), base, logger=logger)
    assert (p.saved, p.failed) == (2, 1)
    names = [os.path.basename(f) for f in emls(base)]
    assert any(n.endswith("_2.eml") for n in names)
    # 실패한 메일은 다음에 다시 시도
    mb.items["inbox"][2]["error"] = None
    p = backup.run_backup(client(url), base, logger=logger)
    assert (p.saved, p.skipped, p.failed) == (1, 2, 0)


def test_log_has_no_password_or_mail_content(server, tmp_path):
    mb, url = server
    fill(mb)
    mb.add("inbox", "C", "비밀제목XYZ", "비밀보낸이", "2026-01-06T08:00:00Z", "<c@x>", error="ErrorItemCorrupt")
    base = str(tmp_path)
    backup.run_backup(client(url), base, logger=backup.setup_logging(base))
    text = "".join(open(f, encoding="utf-8").read() for f in glob.glob(os.path.join(base, "로그", "*.log")))
    assert "완료" in text and "ErrorItemCorrupt" in text
    for secret in ("pw", "kim", "비밀제목XYZ", "비밀보낸이", "회의 일정", "본문", "secret text"):
        assert secret not in text
    # 비밀번호·아이디는 저장 폴더 어디에도 남지 않는다 (메일 원본 제외)
    for root, _, files in os.walk(base):
        if "원본" in root:
            continue
        for f in files:
            data = open(os.path.join(root, f), "rb").read()
            assert b"TOAMASINA" not in data and b"pw" not in data


def test_wrong_password(server, tmp_path):
    _, url = server
    with pytest.raises(ews.LoginError):
        backup.run_backup(client(url, password="wrong"), str(tmp_path))


def test_server_busy_is_retried(server, tmp_path, monkeypatch):
    mb, url = server
    fill(mb)
    monkeypatch.setattr(ews, "RETRY_WAITS", (0.01, 0.01))
    c = client(url)
    c.connect()
    mb.busy_once = True
    assert len(c.find_folders(ews.INBOX)) == 3


def test_connection_problem(tmp_path, monkeypatch):
    monkeypatch.setattr(ews, "RETRY_WAITS", ())
    with pytest.raises(ews.ConnectionProblem):
        backup.run_backup(client("http://127.0.0.1:9/EWS/Exchange.asmx"), str(tmp_path))


def test_stop_and_resume(server, tmp_path):
    mb, url = server
    for i in range(40):
        mb.add("inbox", "M%d" % i, "제목 %d" % i, "보낸이", "2026-01-01T00:00:%02dZ" % i, "<m%d@x>" % i)
    stop = threading.Event()

    def on_progress(p):
        if p.saved >= 10:
            stop.set()

    with pytest.raises(ews.Stopped):
        backup.run_backup(client(url, stop_event=stop), str(tmp_path), on_progress=on_progress,
                          stop_event=stop)
    done = len(emls(str(tmp_path)))
    assert 10 <= done < 40
    p = backup.run_backup(client(url), str(tmp_path))
    assert p.saved == 40 - done and p.skipped == done
    assert len(emls(str(tmp_path))) == 40


def test_names_are_windows_safe():
    assert naming.clean('a<b>c:d"e/f\\g|h?i*j', 80, "x") == "a b c d e f g h i j"
    assert naming.clean("  ...  ", 80, "제목없음") == "제목없음"
    assert naming.clean("CON", 80, "x") == "_CON"
    assert len(naming.clean("가" * 500, 80, "x")) == 80
    assert naming.clean("끝에 점. ", 80, "x") == "끝에 점"
    assert naming.eml_name(None, "", "") == "날짜없음_보낸사람없음_제목없음"


def test_redirect_is_refused():
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.com/")
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ews.UnexpectedServer):
            client("http://127.0.0.1:%d/x" % srv.server_address[1]).connect()
    finally:
        srv.shutdown()


def test_only_one_server_address():
    assert ews.EWS_URL == "https://mail.ambatovy.mg/EWS/Exchange.asmx"
    assert store.item_key("받은편지함", "<a@x>", "ID") != store.item_key("보낸편지함", "<a@x>", "ID")


def test_control_character_in_subject_does_not_stop(server, tmp_path):
    mb, url = server
    mb.add("inbox", "A", "정상 메일", "홍길동", "2026-01-05T08:00:00Z", "<a@x>")
    mb.add("inbox", "B", "이상한\x1f제목", "보낸\x1f이", "2026-01-06T08:00:00Z", "<b@x>")
    mb.add("inbox", "C", "그 다음 메일", "홍길동", "2026-01-07T08:00:00Z", "<c@x>")
    p = backup.run_backup(client(url), str(tmp_path))
    assert (p.saved, p.failed) == (3, 0)


def test_rejected_batch_falls_back_to_single_items(server, tmp_path):
    mb, url = server
    for i in range(5):
        mb.add("inbox", "M%d" % i, "제목 %d" % i, "보낸이", "2026-01-01T00:00:0%dZ" % i, "<m%d@x>" % i)
    mb.fault_ids = {"M2"}
    p = backup.run_backup(client(url), str(tmp_path))
    assert (p.saved, p.failed) == (4, 1)  # 문제 있는 한 통만 건너뜀
    mb.fault_ids = set()
    p = backup.run_backup(client(url), str(tmp_path))
    assert (p.saved, p.skipped, p.failed) == (1, 4, 0)


def test_low_disk_stops_before_download(server, tmp_path, monkeypatch):
    mb, url = server
    fill(mb)
    monkeypatch.setattr(backup, "MIN_FREE_BYTES", 10 ** 18)
    with pytest.raises(backup.LowDisk):
        backup.run_backup(client(url), str(tmp_path))
    assert mb.operations == []  # 서버에 아무 요청도 보내지 않음
    assert emls(str(tmp_path)) == []


def test_single_instance_lock(tmp_path):
    first = store.acquire_lock(str(tmp_path))
    assert first is not None
    assert store.acquire_lock(str(tmp_path)) is None
    first.close()
    again = store.acquire_lock(str(tmp_path))
    assert again is not None
    again.close()


def test_default_folder_and_cloud_detection(monkeypatch, tmp_path):
    assert os.path.basename(store.default_base_dir()) == "MailBackup"
    assert os.path.basename(os.path.dirname(store.default_base_dir())).lower() in ("desktop", "바탕 화면", "바탕화면")
    one = str(tmp_path / "OneDrive")
    monkeypatch.setenv("OneDrive", one)
    assert store.cloud_sync_name(os.path.join(one, "Desktop", "MailBackup")) == "OneDrive"
    assert store.cloud_sync_name(str(tmp_path / "local" / "MailBackup")) == ""
    assert store.cloud_sync_name(str(tmp_path / "OneDrive - Company" / "MailBackup")) == "OneDrive"
    assert store.cloud_sync_name(str(tmp_path / "Dropbox" / "MailBackup")) == "클라우드"


def test_torn_index_line_is_tolerated(server, tmp_path):
    mb, url = server
    fill(mb)
    base = str(tmp_path)
    backup.run_backup(client(url), base)
    with open(os.path.join(base, "상태", "받은목록.jsonl"), "ab") as fh:
        fh.write(b'{"key": "\xec\x9d')  # 줄 중간, 한글 글자 중간에서 끊김
    p = backup.run_backup(client(url), base)
    assert (p.saved, p.skipped) == (0, 5)
    mb.add("inbox", "N", "새 메일", "a", "2026-05-01T00:00:00Z", "<n@x>")
    assert backup.run_backup(client(url), base).saved == 1
    assert backup.run_backup(client(url), base).saved == 0
