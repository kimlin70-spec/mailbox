"""Exchange 웹 서비스(EWS) 읽기 전용 클라이언트.

이 파일이 서버와 통신하는 유일한 곳이다.
서버에 보내는 요청은 아래 세 가지 "조회" 동작뿐이다.

  - FindFolder : 폴더 목록 조회
  - FindItem   : 메일 목록 조회
  - GetItem    : 메일 원본 가져오기

삭제·이동·수정·발송·읽음 표시 같은 쓰기 동작은 코드에 존재하지 않는다.
(ALLOWED_OPERATIONS 밖의 요청은 보내기 전에 차단된다.)
"""

from __future__ import annotations

import base64
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from xml.sax.saxutils import quoteattr

import requests
from requests.auth import HTTPBasicAuth

# 통신 대상은 이 주소 한 곳뿐이다.
EWS_URL = "https://mail.ambatovy.mg/EWS/Exchange.asmx"

ALLOWED_OPERATIONS = ("FindFolder", "FindItem", "GetItem")

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_T = "http://schemas.microsoft.com/exchange/services/2006/types"
NS_M = "http://schemas.microsoft.com/exchange/services/2006/messages"
_T = "{%s}" % NS_T
_M = "{%s}" % NS_M

TIMEOUT = (20, 300)  # (연결, 응답) 초
RETRY_WAITS = (5, 15, 30, 60)  # 일시적 오류 때 다시 시도하기 전 대기(초)
MAX_BUSY_WAIT = 300


class EwsError(Exception):
    """서버가 요청을 거절했을 때. code 는 서버가 준 오류 이름."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LoginError(Exception):
    """아이디 또는 비밀번호가 맞지 않음."""


class CertificateProblem(Exception):
    """서버 보안 인증서를 확인할 수 없음."""


class ConnectionProblem(Exception):
    """서버에 연결할 수 없음."""


class UnexpectedServer(Exception):
    """서버가 예상과 다르게 응답함 (다른 주소로 보내려 함 등)."""


@dataclass(frozen=True)
class FolderRef:
    """폴더를 가리키는 값. kind 는 'distinguished'(기본 폴더) 또는 'id'."""

    kind: str
    value: str

    def xml(self) -> str:
        if self.kind == "distinguished":
            return "<t:DistinguishedFolderId Id=%s/>" % quoteattr(self.value)
        return "<t:FolderId Id=%s/>" % quoteattr(self.value)


INBOX = FolderRef("distinguished", "inbox")
SENT = FolderRef("distinguished", "sentitems")


@dataclass
class FolderInfo:
    folder_id: str
    parent_id: str
    name: str
    folder_class: str


@dataclass
class ItemInfo:
    item_id: str
    subject: str
    sender: str
    received: datetime | None
    size: int
    message_id: str


@dataclass
class MimeResult:
    item_id: str
    content: bytes | None
    error: str  # 성공이면 빈 문자열


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _envelope(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="%s" xmlns:t="%s" xmlns:m="%s">'
        '<soap:Header><t:RequestServerVersion Version="Exchange2010_SP1"/></soap:Header>'
        "<soap:Body>%s</soap:Body></soap:Envelope>" % (NS_SOAP, NS_T, NS_M, body)
    ).encode("utf-8")


class EwsClient:
    def __init__(self, username: str, password: str, url: str = EWS_URL,
                 pause: float = 1.0, sleep=time.sleep, stop_event=None):
        self._username = username
        self._password = password
        self._url = url
        self._pause = pause
        self._sleep_fn = sleep
        self._stop = stop_event
        self._last_request_end = 0.0
        self._session = requests.Session()
        # 시스템 프록시 등 다른 경로를 쓰지 않는다. SSL 검증은 항상 켠다.
        self._session.trust_env = False
        self._session.verify = True
        self._session.headers["User-Agent"] = "MailBackup"
        self.auth_scheme = ""

    # ------------------------------------------------------------------ 연결

    def connect(self) -> None:
        """서버가 어떤 로그인 방식을 쓰는지 확인하고 로그인 준비를 한다."""
        try:
            resp = self._session.get(self._url, allow_redirects=False, timeout=TIMEOUT)
        except requests.exceptions.SSLError as exc:
            raise CertificateProblem() from exc
        except requests.exceptions.RequestException as exc:
            raise ConnectionProblem() from exc
        finally:
            self._last_request_end = time.monotonic()

        if resp.status_code != 401:
            raise UnexpectedServer("HTTP %d" % resp.status_code)

        offered = resp.headers.get("WWW-Authenticate", "").lower()
        if "ntlm" in offered or "negotiate" in offered:
            from requests_ntlm import HttpNtlmAuth

            self._session.auth = HttpNtlmAuth(self._username, self._password)
            self.auth_scheme = "NTLM"
        elif "basic" in offered:
            if not self._url.lower().startswith("https://") and "//127.0.0.1" not in self._url:
                raise UnexpectedServer("insecure basic")
            self._session.auth = HTTPBasicAuth(
                self._username.encode("utf-8"), self._password.encode("utf-8"))
            self.auth_scheme = "Basic"
        else:
            raise UnexpectedServer("no supported auth")

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------ 전송

    def _wait(self, seconds: float) -> None:
        """기다리는 중에도 '중지'를 누르면 바로 멈출 수 있게 잘게 나눠 쉰다."""
        end = time.monotonic() + seconds
        while True:
            if self._stop is not None and self._stop.is_set():
                raise Stopped()
            remain = end - time.monotonic()
            if remain <= 0:
                return
            self._sleep_fn(min(remain, 0.2))

    def _call(self, operation: str, body: str) -> ET.Element:
        if operation not in ALLOWED_OPERATIONS:
            raise RuntimeError("허용되지 않은 서버 요청: " + operation)
        payload = _envelope(body)
        attempt = 0
        while True:
            # 서버에 부담을 주지 않도록 요청 사이에 쉰다.
            since = time.monotonic() - self._last_request_end
            if since < self._pause:
                self._wait(self._pause - since)
            try:
                resp = self._session.post(
                    self._url, data=payload, allow_redirects=False, timeout=TIMEOUT,
                    headers={"Content-Type": "text/xml; charset=utf-8"})
            except requests.exceptions.SSLError as exc:
                raise CertificateProblem() from exc
            except requests.exceptions.RequestException as exc:
                self._last_request_end = time.monotonic()
                if attempt >= len(RETRY_WAITS):
                    raise ConnectionProblem() from exc
                self._wait(RETRY_WAITS[attempt])
                attempt += 1
                continue
            self._last_request_end = time.monotonic()

            if resp.status_code == 401:
                raise LoginError()
            if 300 <= resp.status_code < 400:
                raise UnexpectedServer("HTTP %d" % resp.status_code)

            root = None
            try:
                root = ET.fromstring(resp.content)
            except ET.ParseError:
                pass

            busy_ms = _busy_backoff(root) if root is not None else None
            if busy_ms is not None or resp.status_code in (429, 502, 503, 504):
                if attempt >= len(RETRY_WAITS):
                    raise EwsError("ErrorServerBusy")
                wait = RETRY_WAITS[attempt]
                if busy_ms:
                    wait = max(wait, min(busy_ms / 1000.0, MAX_BUSY_WAIT))
                self._wait(wait)
                attempt += 1
                continue

            if root is None:
                raise UnexpectedServer("HTTP %d" % resp.status_code)
            fault = root.find(".//{%s}Fault" % NS_SOAP)
            if fault is not None or resp.status_code != 200:
                raise EwsError(_fault_code(root) or "HTTP %d" % resp.status_code)
            return root

    # ------------------------------------------------------------------ 조회

    def find_folders(self, parent: FolderRef) -> list[FolderInfo]:
        """parent 아래의 모든 하위 폴더(여러 단계 포함)를 조회한다."""
        result: list[FolderInfo] = []
        offset = 0
        while True:
            body = (
                '<m:FindFolder Traversal="Deep"><m:FolderShape>'
                "<t:BaseShape>IdOnly</t:BaseShape><t:AdditionalProperties>"
                '<t:FieldURI FieldURI="folder:DisplayName"/>'
                '<t:FieldURI FieldURI="folder:ParentFolderId"/>'
                '<t:FieldURI FieldURI="folder:FolderClass"/>'
                "</t:AdditionalProperties></m:FolderShape>"
                '<m:IndexedPageFolderView MaxEntriesReturned="100" Offset="%d" BasePoint="Beginning"/>'
                "<m:ParentFolderIds>%s</m:ParentFolderIds></m:FindFolder>" % (offset, parent.xml())
            )
            root = self._call("FindFolder", body)
            msg = _single_message(root, "FindFolderResponseMessage")
            rf = msg.find(_M + "RootFolder")
            folders = rf.find(_T + "Folders") if rf is not None else None
            count = 0
            for el in (folders if folders is not None else []):
                count += 1
                fid = el.find(_T + "FolderId")
                pid = el.find(_T + "ParentFolderId")
                if fid is None:
                    continue
                result.append(FolderInfo(
                    folder_id=fid.get("Id", ""),
                    parent_id=pid.get("Id", "") if pid is not None else "",
                    name=el.findtext(_T + "DisplayName") or "",
                    folder_class=el.findtext(_T + "FolderClass") or "",
                ))
            offset += count
            if rf is None or count == 0 or rf.get("IncludesLastItemInRange", "true") == "true":
                return result

    def find_items(self, folder: FolderRef, offset: int, limit: int,
                   since: datetime | None = None) -> tuple[list[ItemInfo], int, bool]:
        """폴더 안의 메일 목록 한 쪽을 조회한다. (목록, 전체 개수, 마지막 쪽 여부)"""
        restriction = ""
        if since is not None:
            restriction = (
                "<m:Restriction><t:IsGreaterThanOrEqualTo>"
                '<t:FieldURI FieldURI="item:DateTimeReceived"/>'
                "<t:FieldURIOrConstant><t:Constant Value=%s/></t:FieldURIOrConstant>"
                "</t:IsGreaterThanOrEqualTo></m:Restriction>"
                % quoteattr(since.strftime("%Y-%m-%dT%H:%M:%SZ"))
            )
        body = (
            '<m:FindItem Traversal="Shallow"><m:ItemShape>'
            "<t:BaseShape>IdOnly</t:BaseShape><t:AdditionalProperties>"
            '<t:FieldURI FieldURI="item:Subject"/>'
            '<t:FieldURI FieldURI="item:DateTimeReceived"/>'
            '<t:FieldURI FieldURI="item:Size"/>'
            '<t:FieldURI FieldURI="message:From"/>'
            '<t:FieldURI FieldURI="message:InternetMessageId"/>'
            "</t:AdditionalProperties></m:ItemShape>"
            '<m:IndexedPageItemView MaxEntriesReturned="%d" Offset="%d" BasePoint="Beginning"/>'
            "%s"
            '<m:SortOrder><t:FieldOrder Order="Ascending">'
            '<t:FieldURI FieldURI="item:DateTimeReceived"/></t:FieldOrder></m:SortOrder>'
            "<m:ParentFolderIds>%s</m:ParentFolderIds></m:FindItem>"
            % (limit, offset, restriction, folder.xml())
        )
        root = self._call("FindItem", body)
        msg = _single_message(root, "FindItemResponseMessage")
        rf = msg.find(_M + "RootFolder")
        if rf is None:
            return [], 0, True
        items: list[ItemInfo] = []
        container = rf.find(_T + "Items")
        for el in (container if container is not None else []):
            iid = el.find(_T + "ItemId")
            if iid is None:
                continue
            try:
                size = int(el.findtext(_T + "Size") or 0)
            except ValueError:
                size = 0
            items.append(ItemInfo(
                item_id=iid.get("Id", ""),
                subject=el.findtext(_T + "Subject") or "",
                sender=el.findtext("%sFrom/%sMailbox/%sName" % (_T, _T, _T))
                or el.findtext("%sFrom/%sMailbox/%sEmailAddress" % (_T, _T, _T)) or "",
                received=_parse_dt(el.findtext(_T + "DateTimeReceived")),
                size=size,
                message_id=(el.findtext(_T + "InternetMessageId") or "").strip(),
            ))
        try:
            total = int(rf.get("TotalItemsInView", "0"))
        except ValueError:
            total = 0
        last = rf.get("IncludesLastItemInRange", "true") == "true"
        return items, total, last

    def get_mime(self, item_ids: list[str]) -> list[MimeResult]:
        """메일 원본(.eml 내용)을 가져온다. 읽음 상태는 바뀌지 않는다."""
        ids = "".join("<t:ItemId Id=%s/>" % quoteattr(i) for i in item_ids)
        body = (
            "<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape>"
            "<t:IncludeMimeContent>true</t:IncludeMimeContent></m:ItemShape>"
            "<m:ItemIds>%s</m:ItemIds></m:GetItem>" % ids
        )
        root = self._call("GetItem", body)
        messages = root.findall(".//" + _M + "GetItemResponseMessage")
        results: list[MimeResult] = []
        for idx, item_id in enumerate(item_ids):
            if idx >= len(messages):
                results.append(MimeResult(item_id, None, "NoResponse"))
                continue
            msg = messages[idx]
            if msg.get("ResponseClass") == "Error":
                results.append(MimeResult(item_id, None, msg.findtext(_M + "ResponseCode") or "Error"))
                continue
            mime = msg.find(".//" + _T + "MimeContent")
            if mime is None or mime.text is None:
                results.append(MimeResult(item_id, None, "NoMimeContent"))
                continue
            try:
                results.append(MimeResult(item_id, base64.b64decode(mime.text), ""))
            except ValueError:
                results.append(MimeResult(item_id, None, "BadMimeContent"))
        return results


class Stopped(Exception):
    """사용자가 '중지'를 누름."""


def _single_message(root: ET.Element, tag: str) -> ET.Element:
    msg = root.find(".//" + _M + tag)
    if msg is None:
        raise EwsError(_fault_code(root) or "NoResponseMessage")
    if msg.get("ResponseClass") == "Error":
        raise EwsError(msg.findtext(_M + "ResponseCode") or "Error")
    return msg


_CODE_RE = re.compile(r"^[A-Za-z0-9_.:\- ]{1,80}$")


def _fault_code(root: ET.Element) -> str:
    """오류 '이름'만 꺼낸다. (설명 문장은 로그에 남기지 않기 위해 쓰지 않는다.)"""
    for path in (".//{%s}ResponseCode" % "http://schemas.microsoft.com/exchange/services/2006/errors",
                 ".//" + _M + "ResponseCode", ".//faultcode"):
        text = root.findtext(path)
        if text and _CODE_RE.match(text.strip()):
            return text.strip()
    return ""


def _busy_backoff(root: ET.Element) -> int | None:
    """서버가 '바쁘니 잠시 후 다시'라고 했으면 기다릴 시간(ms)을, 아니면 None."""
    busy = False
    for el in root.iter():
        if el.tag.endswith("}ResponseCode") and (el.text or "").strip() == "ErrorServerBusy":
            busy = True
            break
    if not busy:
        return None
    for el in root.iter():
        if el.get("Name") == "BackOffMilliseconds":
            try:
                return int((el.text or "0").strip())
            except ValueError:
                return 0
    return 0
