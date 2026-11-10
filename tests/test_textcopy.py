import csv
import glob
import os
from email.message import EmailMessage

from mailbackup import cleaner, textcopy

OUTLOOK_QUOTE = """
________________________________
From: 홍길동 <hong@ambatovy.mg>
Sent: Monday, January 5, 2026 11:00 AM
To: Élodie Dupont <elodie@ambatovy.mg>
Subject: 보일러 점검 일정

"""

DISCLAIMER = ("This e-mail and any attachments are confidential and intended solely for the addressee. "
              "If you are not the intended recipient, please delete it.")


def make(base, folder, name, subject, sender, to, date, body, mid, reply_to=None, cc=None,
         attach=None, html=None, inline_logo=False, charset="utf-8"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Date"] = date
    msg["Message-ID"] = mid
    if reply_to:
        msg["In-Reply-To"] = reply_to
        msg["References"] = reply_to
    if body is not None:
        msg.set_content(body, charset=charset)
    if html is not None:
        if body is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    if inline_logo:
        msg.add_attachment(b"\x89PNGlogo", maintype="image", subtype="png", filename="logo.png",
                           disposition="inline", cid="<logo1>")
    for fname, data in (attach or []):
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=fname)
    d = os.path.join(base, "원본", folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name + ".eml"), "wb") as fh:
        fh.write(msg.as_bytes())


def first_mail(base):
    body1 = ("엘로디 님,\n\n보일러 점검을 1월 12일에 진행하려 합니다. 가능 여부 알려 주세요.\n"
             "Merci de confirmer la disponibilité de l'équipe.\n\n감사합니다.\n홍길동 과장\n"
             "발전팀 / Tel. +261 20 00 000 00\n\n" + DISCLAIMER)
    make(base, "받은편지함/2026-01", "m1", "보일러 점검 일정", "홍길동 <hong@ambatovy.mg>",
         "Élodie Dupont <elodie@ambatovy.mg>", "Mon, 05 Jan 2026 08:00:00 +0000", body1, "<m1@x>",
         cc="John Smith <john@ambatovy.mg>", attach=[("점검 계획서.pdf", b"%PDF-plan")], inline_logo=True)
    return body1


def reply_mail(base, body1):
    body2 = ("Bonjour M. Hong,\n\nC'est noté, l'équipe sera disponible le 12 janvier.\n\nCordialement,\n"
             "Élodie Dupont\nIngénieure maintenance\n" + OUTLOOK_QUOTE + body1)
    make(base, "받은편지함/2026-01", "m2", "RE: 보일러 점검 일정", "Élodie Dupont <elodie@ambatovy.mg>",
         "홍길동 <hong@ambatovy.mg>", "Tue, 06 Jan 2026 09:00:00 +0000", body2, "<m2@x>", reply_to="<m1@x>")


def read(base, pattern):
    files = glob.glob(os.path.join(base, "텍스트", "**", pattern), recursive=True)
    assert len(files) == 1, files
    return files[0], open(files[0], encoding="utf-8").read()


def test_thread_file_cleaning_attachments_csv(tmp_path):
    base = str(tmp_path)
    body1 = first_mail(base)
    reply_mail(base, body1)
    make(base, "보낸편지함/2026-01", "other", "Shutdown plan", "me@ambatovy.mg", "a@b.c",
         "Wed, 07 Jan 2026 09:00:00 +0000", "Please find the plan.", "<o1@x>")

    res = textcopy.run_textcopy(base)
    assert (res.mails, res.new_mails, res.threads, res.written, res.failed) == (3, 3, 2, 2, 0)

    path, md = read(base, "보일러 점검 일정.md")
    assert os.sep + "2026-01" + os.sep in path
    assert md.startswith("# 보일러 점검 일정\n")
    assert "- 메일 수: 2" in md
    # 시간순, 메일마다 날짜·보낸사람·받는사람·참조·제목
    assert md.index("가능 여부 알려 주세요") < md.index("C'est noté")
    assert "- 보낸사람: 홍길동 <hong@ambatovy.mg>" in md
    assert "- 받는사람: Élodie Dupont <elodie@ambatovy.mg>" in md
    assert "- 참조: John Smith <john@ambatovy.mg>" in md
    assert "- 제목: RE: 보일러 점검 일정" in md
    # 앞선 메일과 겹치는 인용만 제거, 서명·면책 문구는 그대로
    assert md.count("가능 여부 알려 주세요") == 1
    assert "Sent: Monday" not in md
    assert md.count("confidential") == 1
    assert "Tel. +261" in md and "Ingénieure maintenance" in md
    assert "감사합니다." in md and "Cordialement," in md
    # 첨부: 파일명만 적고 파일은 꺼내지 않는다 (서명 로고는 목록에서도 제외)
    assert "- 첨부: 점검 계획서.pdf (원본 메일을 열어서 확인)" in md and "%PDF" not in md
    assert "logo.png" not in md
    assert "- 원본 메일: " in md and "m1.eml" in md
    assert not os.path.exists(os.path.join(base, "첨부"))

    # CSV: UTF-8(BOM), 마지막 날짜순
    raw = open(os.path.join(base, "대화목록.csv"), "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
    assert rows[0] == ["마지막 날짜", "제목", "참여자", "메일 수", "파일위치"]
    assert rows[1][1] == "Shutdown plan" and rows[2][1] == "보일러 점검 일정"
    assert rows[2][3] == "2" and "홍길동" in rows[2][2] and "Élodie Dupont" in rows[2][2]
    assert os.path.exists(os.path.join(base, rows[2][4]))

    # 다시 실행: 바뀐 것 없음
    res = textcopy.run_textcopy(base)
    assert (res.new_mails, res.written, res.threads) == (0, 0, 2)


def test_new_mail_updates_thread_and_moves_month(tmp_path):
    base = str(tmp_path)
    body1 = first_mail(base)
    textcopy.run_textcopy(base)
    old_path, _ = read(base, "보일러 점검 일정.md")

    reply_mail(base, body1)
    make(base, "받은편지함/2026-02", "m3", "RE: 보일러 점검 일정", "홍길동 <hong@ambatovy.mg>",
         "Élodie Dupont <elodie@ambatovy.mg>", "Mon, 02 Feb 2026 09:00:00 +0000", "완료했습니다.", "<m3@x>",
         reply_to="<m2@x>")
    res = textcopy.run_textcopy(base)
    assert (res.new_mails, res.written, res.threads) == (2, 1, 1)
    new_path, md = read(base, "보일러 점검 일정*.md")
    assert os.sep + "2026-02" + os.sep in new_path and not os.path.exists(old_path)
    assert "- 메일 수: 3" in md and "완료했습니다." in md
    assert not os.path.exists(os.path.dirname(old_path))  # 빈 월 폴더 정리
    rows = list(csv.reader(open(os.path.join(base, "대화목록.csv"), encoding="utf-8-sig")))
    assert len(rows) == 2 and rows[1][3] == "3"


def test_unverified_quote_is_kept(tmp_path):
    """앞선 메일이 백업에 없으면 인용을 지우지 않는다 (애매하면 남김)."""
    base = str(tmp_path)
    body = "FYI\n" + OUTLOOK_QUOTE + "외부에서 받은 중요한 내용입니다. 꼭 확인해 주세요."
    make(base, "받은편지함/2026-01", "f1", "FW: 보일러 점검 일정", "a@b.c", "me@b.c",
         "Mon, 05 Jan 2026 08:00:00 +0000", body, "<f1@x>")
    textcopy.run_textcopy(base)
    _, md = read(base, "*.md")
    assert "외부에서 받은 중요한 내용입니다" in md and "FYI" in md


def test_same_subject_different_threads_and_duplicates(tmp_path):
    base = str(tmp_path)
    for i in (1, 2):
        make(base, "받은편지함/2026-01", "d%d" % i, "Daily report", "a@b.c", "me@b.c",
             "Mon, 0%d Jan 2026 08:00:00 +0000" % (4 + i), "Report %d" % i, "<d%d@x>" % i)
    # 같은 메일이 받은편지함·보낸편지함 양쪽에
    make(base, "보낸편지함/2026-01", "d1copy", "Daily report", "a@b.c", "me@b.c",
         "Mon, 05 Jan 2026 08:00:00 +0000", "Report 1", "<d1@x>")
    res = textcopy.run_textcopy(base)
    assert res.threads == 2
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(base, "텍스트", "*", "*.md")))
    assert names == ["Daily report.md", "Daily report_2.md"]
    for p in glob.glob(os.path.join(base, "텍스트", "*", "*.md")):
        assert "- 메일 수: 1" in open(p, encoding="utf-8").read()


def test_html_only_and_legacy_charsets(tmp_path):
    base = str(tmp_path)
    make(base, "받은편지함/2026-01", "h1", "HTML 메일", "a@b.c", "me@b.c",
         "Mon, 05 Jan 2026 08:00:00 +0000", None, "<h1@x>",
         html="<html><head><style>p{color:red}</style></head><body><p>Bonjour à tous,</p>"
              "<p>회의는 <b>3시</b>입니다.</p><table><tr><td>항목</td><td>값</td></tr></table></body></html>")
    make(base, "받은편지함/2026-01", "k1", "한글 인코딩", "a@b.c", "me@b.c",
         "Mon, 05 Jan 2026 09:00:00 +0000", "옛날 방식 한글 메일입니다.", "<k1@x>", charset="euc-kr")
    make(base, "받은편지함/2026-01", "l1", "Français", "a@b.c", "me@b.c",
         "Mon, 05 Jan 2026 10:00:00 +0000", "Réunion reportée à l'été.", "<l1@x>", charset="iso-8859-1")
    textcopy.run_textcopy(base)
    _, md = read(base, "HTML 메일.md")
    assert "Bonjour à tous," in md and "회의는 3시입니다." in md and "항목 | 값" in md
    assert "color:red" not in md
    assert "옛날 방식 한글 메일입니다." in read(base, "한글 인코딩.md")[1]
    assert "Réunion reportée à l'été." in read(base, "Français.md")[1]


def test_broken_file_does_not_stop(tmp_path):
    base = str(tmp_path)
    first_mail(base)
    with open(os.path.join(base, "원본", "받은편지함", "2026-01", "broken.eml"), "wb") as fh:
        fh.write(b"\xff\xfe\x00garbage")
    res = textcopy.run_textcopy(base)
    assert res.threads >= 1 and os.path.exists(os.path.join(base, "대화목록.csv"))


def test_cleaner_rules():
    # 서명·면책 문구·추신은 건드리지 않는다
    text = "본문입니다.\n\nBest regards,\nKim\nTel. 010-0000-0000\n\nThis e-mail is confidential and intended solely for the addressee."
    assert cleaner.clean_body(text, []) == text
    assert cleaner.clean_body(text, [cleaner.squash("다른 메일")]) == text
    # Gmail 식 인용
    earlier = [cleaner.squash("원래 메일의 내용이 여기에 충분히 길게 있습니다.")]
    reply = "네 알겠습니다.\n\n2026년 1월 5일 (월) 오후 3:00, 홍길동 <a@b>님이 작성:\n> 원래 메일의 내용이 여기에 충분히 길게 있습니다."
    assert cleaner.clean_body(reply, earlier) == "네 알겠습니다."
    assert "원래 메일" in cleaner.clean_body(reply, [])
    assert textcopy.normalize_subject("RE: FW: TR: 회신: 제목") == ("제목", True)
    assert textcopy.normalize_subject("Report: Q1") == ("Report: Q1", False)
    # 빈 칸이 많은 표도 바로 처리된다
    import time
    t0 = time.time()
    out = cleaner.html_to_text("<table><tr>" + "<td></td>" * 60 + "<td>값</td></tr></table>")
    assert "값" in out and time.time() - t0 < 1


def test_orphan_reply_joins_by_subject(tmp_path):
    base = str(tmp_path)
    make(base, "받은편지함/2026-01", "o1", "예산 검토", "a@b.c", "me@b.c",
         "Mon, 05 Jan 2026 08:00:00 +0000", "검토 부탁드립니다.", "<o1@x>")
    make(base, "받은편지함/2026-01", "o2", "RE: 예산 검토", "me@b.c", "a@b.c",
         "Mon, 05 Jan 2026 09:00:00 +0000", "확인했습니다.", "<o2@x>")  # 연결 정보 없음
    assert textcopy.run_textcopy(base).threads == 1


def test_inline_reply_below_quote_is_kept(tmp_path):
    """인용 사이·아래에 새로 적은 답변이 있으면 인용을 지우지 않는다."""
    base = str(tmp_path)
    body1 = "질문 1: 점검 일정은 언제가 좋을까요?\n질문 2: 필요한 인원은 몇 명입니까?"
    make(base, "받은편지함/2026-01", "q1", "질문", "a@b.c", "me@b.c",
         "Mon, 05 Jan 2026 08:00:00 +0000", body1, "<q1@x>")
    body2 = ("아래에 답변 적었습니다.\n\n-----Original Message-----\nFrom: a@b.c\nSent: Monday\nTo: me@b.c\n"
             "Subject: 질문\n\n질문 1: 점검 일정은 언제가 좋을까요?\n=> 다음 주 화요일이 가장 좋겠습니다.\n"
             "질문 2: 필요한 인원은 몇 명입니까?\n=> 세 명이면 충분합니다.")
    make(base, "받은편지함/2026-01", "q2", "RE: 질문", "me@b.c", "a@b.c",
         "Mon, 05 Jan 2026 09:00:00 +0000", body2, "<q2@x>", reply_to="<q1@x>")
    textcopy.run_textcopy(base)
    _, md = read(base, "질문.md")
    assert "다음 주 화요일이 가장 좋겠습니다" in md and "세 명이면 충분합니다" in md


def test_upgrade_removes_extracted_attachments_and_old_text(tmp_path):
    import json
    base = str(tmp_path)
    first_mail(base)
    os.makedirs(os.path.join(base, "상태"))
    os.makedirs(os.path.join(base, "첨부", "2026-01"))
    os.makedirs(os.path.join(base, "텍스트", "2026-01"))
    att = os.path.join("첨부", "2026-01", "2026-01-05_0800_견적서.pdf.exe")
    mine = os.path.join(base, "첨부", "2026-01", "내가 넣은 파일.txt")
    for path in (os.path.join(base, att), mine, os.path.join(base, "텍스트", "2026-01", "보일러 점검 일정.md")):
        open(path, "w").close()
    with open(os.path.join(base, "상태", "텍스트색인_v1.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"path": "x", "atts": [["견적서.pdf.exe", att], ["x", "../../outside.txt"]]}) + "\n")
    with open(os.path.join(base, "상태", "대화파일.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "files": {os.path.join("텍스트", "2026-01", "보일러 점검 일정.md"): ["<m1@x>"]}}, fh)
    textcopy.run_textcopy(base)
    assert not os.path.exists(os.path.join(base, att))      # 프로그램이 꺼냈던 첨부는 정리
    assert os.path.exists(mine)                              # 사용자가 넣은 파일은 그대로
    assert not os.path.exists(os.path.join(base, "상태", "텍스트색인_v1.jsonl"))
    names = os.listdir(os.path.join(base, "텍스트", "2026-01"))
    assert names == ["보일러 점검 일정.md"]                   # _2 중복 없이 새로 만들어짐
    assert "원본 메일" in open(os.path.join(base, "텍스트", "2026-01", names[0]), encoding="utf-8").read()


def test_empty_source_keeps_existing_text(tmp_path):
    import shutil
    base = str(tmp_path)
    first_mail(base)
    textcopy.run_textcopy(base)
    path, _ = read(base, "*.md")
    shutil.move(os.path.join(base, "원본"), os.path.join(base, "원본_옮김"))
    res = textcopy.run_textcopy(base)
    assert res.no_source and os.path.exists(path)
