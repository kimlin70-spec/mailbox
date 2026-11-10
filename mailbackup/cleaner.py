"""메일 본문 정리: 이전 메일 인용, 서명, 면책 문구 제거.

원칙: 애매하면 남긴다.
  - 인용은 "같은 대화의 앞선 메일에 실제로 들어 있는 내용"으로 확인될 때만 지운다.
  - 서명·면책 문구는 본문 끝부분의 짧은 덩어리일 때만 지운다.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# ------------------------------------------------------------ HTML → 텍스트

_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote",
          "hr", "ul", "ol", "pre"}
_SKIP = {"style", "script", "head", "title"}


class _Html2Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data.replace("\r", "").replace("\n", " "))


def html_to_text(html: str) -> str:
    parser = _Html2Text()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    text = "".join(parser.out).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    lines = [ln.rstrip(" |") for ln in lines]
    return tidy("\n".join(lines))


def tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(ln.rstrip() for ln in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ------------------------------------------------------------ 이전 메일 인용

_RE_ORIG = re.compile(
    r"^\s*-{2,}\s*(original message|원본 메시지|원본 메일|message d'origine|forwarded message|"
    r"전달된 메시지|message transféré)\s*-{2,}\s*$", re.I)
_RE_FROM = re.compile(r"^\s*\*{0,2}(from|de|보낸 사람|보낸사람|발신)\s*:\*{0,2}\s*\S", re.I)
_RE_HDR = re.compile(
    r"^\s*\*{0,2}(sent|envoyé|date|보낸 날짜|날짜|to|à|a|받는 사람|받는사람|subject|objet|제목|cc|참조)"
    r"\s*:", re.I)
_RE_WROTE = re.compile(r"^\s*(on\s.+\swrote|le\s.+\sa écrit|\d{4}년\s.+작성|.+님이 작성)\s*:?\s*$", re.I)
_RE_RULE = re.compile(r"^[\s_\-=*]*$")


def find_quote_start(lines: list[str]) -> int | None:
    """이전 메일 인용이 시작되는 줄 번호. 없으면 None."""
    n = len(lines)
    for i, line in enumerate(lines):
        if _RE_ORIG.match(line):
            return i
        if _RE_FROM.match(line):
            following = [ln for ln in lines[i + 1:i + 8] if ln.strip()][:5]
            if sum(1 for ln in following if _RE_HDR.match(ln)) >= 2:
                return i
        if _RE_WROTE.match(line):
            return i
        if i + 1 < n and line.strip() and _RE_WROTE.match(line.strip() + " " + lines[i + 1].strip()) \
                and re.match(r"^\s*(on|le|\d{4}년)\s", line, re.I):
            return i
    # '>' 로 시작하는 줄이 끝까지 이어지는 경우
    start = None
    count = 0
    for i in range(n - 1, -1, -1):
        s = lines[i].strip()
        if not s:
            continue
        if s.startswith(">"):
            start, count = i, count + 1
        else:
            break
    return start if count >= 2 else None


def squash(text: str) -> str:
    """비교용: 공백·인용 기호를 없애고 소문자로."""
    return re.sub(r"[\s>*|]+", "", text).lower()


def _is_header_line(ln: str) -> bool:
    return bool(_RE_ORIG.match(ln) or _RE_FROM.match(ln) or _RE_HDR.match(ln) or _RE_WROTE.match(ln)
                or _RE_RULE.match(ln))


def strip_quote(text: str, earlier_squashed: list[str]) -> str:
    """이전 메일 인용을 지운다.

    인용 표시 아래의 모든 줄이 같은 대화의 앞선 메일에 실제로 들어 있을 때만 지운다.
    한 줄이라도 새 내용(인용 사이에 적은 답변 등)이 있으면 전부 남긴다.
    """
    if not earlier_squashed:
        return text
    lines = text.split("\n")
    start = find_quote_start(lines)
    if start is None:
        return text
    for ln in lines[start:]:
        if _is_header_line(ln):
            continue
        s = squash(ln)
        if not s:
            continue
        if not any(s in e for e in earlier_squashed):
            return text  # 확인되지 않는 줄이 있음 → 남긴다
    kept = lines[:start]
    while kept and _RE_RULE.match(kept[-1]):
        kept.pop()
    return "\n".join(kept)


# ------------------------------------------------------------ 면책 문구·서명

_RE_DISCLAIMER = re.compile(
    r"(confidentiality notice|avis de confidentialit|^\s*disclaimer\b|"
    r"this (e-?mail|message|communication|transmission)\b|"
    r"ce (message|courriel|e-?mail|courrier)\b|"
    r"본 (메일|이메일|전자우편|메시지))", re.I | re.M)
# 면책 문구로 보려면 아래 세 묶음 중 두 가지 이상이 들어 있어야 한다.
_DISCLAIMER_SIGNS = (
    re.compile(r"confidential|confidentiel|privileged|기밀|비밀", re.I),
    re.compile(r"intended|recipient|addressee|destinataire|수신인|수신자", re.I),
    re.compile(r"delete|destroy|notify|unauthori[sz]ed|supprimer|détruire|interdite|삭제|통보|금지", re.I),
)
_RE_ECO = re.compile(
    r"^\s*(please consider the environment before printing.*|"
    r"pensez à l'environnement avant d'imprimer.*)$", re.I)
_RE_MOBILE = re.compile(
    r"^\s*(sent from my \w+.*|envoyé (de|depuis) mon \w+.*|나의 \w+에서 보냄.*|get outlook for \w+.*|"
    r"télécharger outlook pour \w+.*|sent from outlook.*|outlook for (ios|android).*)$", re.I)
_RE_CLOSING = re.compile(
    r"^\s*(best regards|kind regards|warm regards|regards|sincerely|yours sincerely|thanks and regards|"
    r"thanks & regards|br|rgds|cordialement|bien cordialement|très cordialement|bien à vous|"
    r"salutations|sincères salutations|meilleures salutations|감사합니다|고맙습니다|수고하세요|"
    r"수고하십시오)[\s,.!~]*$", re.I)
_RE_PS = re.compile(r"^\s*(p\.?\s?s\.?\b|추신)", re.I)
_RE_SENTENCE_END = re.compile(r"([.?!]|니다|세요|어요|아요|습니까)\s*$")


def strip_disclaimer(text: str) -> str:
    """본문 맨 끝의 면책 문구만 지운다."""
    for m in _RE_DISCLAIMER.finditer(text):
        start = text.rfind("\n", 0, m.start()) + 1
        tail = text[start:]
        if len(tail) > 1500 or start < len(text) * 0.5 and len(text) > 400:
            continue
        if sum(1 for sign in _DISCLAIMER_SIGNS if sign.search(tail)) < 2:
            continue
        if find_quote_start(tail.split("\n")) is not None:
            continue  # 뒤에 (남겨 둔) 인용이 있으면 건드리지 않는다
        return text[:start].rstrip()
    return text


def _looks_like_signature(tail: list[str]) -> bool:
    """이름·직함·연락처처럼 짧은 줄 몇 개뿐인지. 문장이 있으면 서명이 아니다."""
    lines = [ln.strip() for ln in tail if ln.strip()]
    if not lines or len(lines) > 6:
        return False
    for ln in lines:
        if len(ln) > 60 or _RE_PS.match(ln):
            return False
        has_contact = bool(re.search(r"\d{3}|@|www\.|https?:", ln))
        if not has_contact and _RE_SENTENCE_END.search(ln):
            return False
    return True


def strip_signature(text: str) -> str:
    lines = [ln for ln in text.split("\n") if not _RE_ECO.match(ln)]
    # 휴대폰 꼬리말
    while lines and (not lines[-1].strip() or _RE_MOBILE.match(lines[-1])):
        lines.pop()
    # 표준 서명 구분선 "-- "
    for i in range(len(lines) - 1, max(-1, len(lines) - 10), -1):
        if lines[i] in ("-- ", "--") and _looks_like_signature(lines[i + 1:]):
            lines = lines[:i]
            break
    # 맺음말 뒤의 짧은 이름·직함·연락처
    for i in range(len(lines) - 1, max(-1, len(lines) - 10), -1):
        if _RE_CLOSING.match(lines[i]):
            if i > 0 and _looks_like_signature(lines[i + 1:]):
                lines = lines[:i + 1]
            break
    return "\n".join(lines).rstrip()


def clean_body(text: str, earlier_squashed: list[str]) -> str:
    text = tidy(text)
    text = strip_quote(text, earlier_squashed)
    text = strip_disclaimer(text)
    text = strip_signature(text)
    return tidy(text)
