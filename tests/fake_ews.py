"""시험용 가짜 Exchange 서버 (이 PC 안에서만 동작)."""

from __future__ import annotations

import base64
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape as _escape, quoteattr


def escape(text):
    # Exchange 는 제어문자를 &#x1F; 같은 형태로 보낸다 (XML 규격상 잘못된 문자)
    return _escape(text).replace("\x1f", "&#x1F;")

NS = ('xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
      'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types" '
      'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"')


def env(body: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?><s:Envelope %s><s:Body>%s</s:Body></s:Envelope>'
            % (NS, body)).encode("utf-8")


class _Fault(Exception):
    pass


class FakeMailbox:
    def __init__(self, user="TOAMASINA\\kim", password="pw"):
        self.user, self.password = user, password
        # folder id -> (parent id, name, class)
        self.folders = {
            "F-proj": ("inbox", "프로젝트: A/B", "IPF.Note"),
            "F-sub": ("F-proj", "Réunions", "IPF.Note"),
            "F-cal": ("inbox", "일정", "IPF.Appointment"),
        }
        # folder id -> list of dict(id, subject, sender, received, mid, mime, error)
        self.items = {"inbox": [], "sentitems": [], "F-proj": [], "F-sub": [], "F-cal": []}
        self.operations: list[str] = []
        self.busy_once = False
        self.get_item_calls: list[int] = []
        self.fault_ids: set[str] = set()  # 이 메일이 포함된 요청은 통째로 거절

    def add(self, folder, item_id, subject, sender, received, mid=None, body="본문", error=None):
        mime = ("From: %s\r\nSubject: =?utf-8?b?%s?=\r\nMessage-ID: %s\r\n\r\n%s" % (
            sender, base64.b64encode(subject.encode()).decode(), mid or "", body)).encode("utf-8")
        self.items[folder].append(dict(id=item_id, subject=subject, sender=sender,
                                       received=received, mid=mid, mime=mime, error=error))

    # -- 응답 만들기

    def handle(self, xml: str) -> tuple[int, bytes]:
        op = re.search(r"<m:(\w+)[ >]", xml).group(1)
        self.operations.append(op)
        if self.busy_once:
            self.busy_once = False
            return 500, env(
                '<s:Fault><faultcode>a:ErrorServerBusy</faultcode><detail>'
                '<e:ResponseCode xmlns:e="http://schemas.microsoft.com/exchange/services/2006/errors">'
                'ErrorServerBusy</e:ResponseCode>'
                '<t:MessageXml><t:Value Name="BackOffMilliseconds">100</t:Value></t:MessageXml>'
                '</detail></s:Fault>')
        try:
            return 200, getattr(self, "op_" + op)(xml)
        except _Fault:
            return 500, env(
                '<s:Fault><faultcode>a:ErrorInternalServerError</faultcode><detail>'
                '<e:ResponseCode xmlns:e="http://schemas.microsoft.com/exchange/services/2006/errors">'
                'ErrorInternalServerError</e:ResponseCode></detail></s:Fault>')

    @staticmethod
    def _parent(xml: str) -> str:
        m = re.search(r'<t:(?:Distinguished)?FolderId Id="([^"]+)"', xml)
        return m.group(1)

    def op_FindFolder(self, xml):
        parent = self._parent(xml)
        offset = int(re.search(r'Offset="(\d+)"', xml).group(1))

        def under(fid):
            while fid in self.folders:
                fid = self.folders[fid][0]
                if fid == parent:
                    return True
            return False

        all_ids = [f for f in self.folders if under(f)]
        page = all_ids[offset:offset + 2]  # 쪽 나눔을 시험하려고 일부러 2개씩
        last = offset + len(page) >= len(all_ids)
        parts = "".join(
            "<t:Folder><t:FolderId Id=%s/><t:ParentFolderId Id=%s/><t:FolderClass>%s</t:FolderClass>"
            "<t:DisplayName>%s</t:DisplayName></t:Folder>" % (
                quoteattr(f), quoteattr("ROOT-" + parent if self.folders[f][0] == parent else self.folders[f][0]),
                self.folders[f][2], escape(self.folders[f][1])) for f in page)
        return env('<m:FindFolderResponse><m:ResponseMessages><m:FindFolderResponseMessage ResponseClass="Success">'
                   '<m:ResponseCode>NoError</m:ResponseCode>'
                   '<m:RootFolder TotalItemsInView="%d" IncludesLastItemInRange="%s"><t:Folders>%s</t:Folders>'
                   '</m:RootFolder></m:FindFolderResponseMessage></m:ResponseMessages></m:FindFolderResponse>'
                   % (len(all_ids), str(last).lower(), parts))

    def op_FindItem(self, xml):
        folder = self._parent(xml[xml.index("<m:ParentFolderIds>"):])
        offset = int(re.search(r'Offset="(\d+)"', xml).group(1))
        limit = int(re.search(r'MaxEntriesReturned="(\d+)"', xml).group(1))
        items = sorted(self.items[folder], key=lambda i: i["received"])
        m = re.search(r'<t:Constant Value="([^"]+)"', xml)
        if m:
            items = [i for i in items if i["received"] >= m.group(1)]
        page = items[offset:offset + limit]
        last = offset + len(page) >= len(items)
        parts = "".join(
            "<t:Message><t:ItemId Id=%s ChangeKey=\"ck\"/><t:Subject>%s</t:Subject>"
            "<t:DateTimeReceived>%s</t:DateTimeReceived><t:Size>%d</t:Size>"
            "<t:From><t:Mailbox><t:Name>%s</t:Name></t:Mailbox></t:From>%s</t:Message>" % (
                quoteattr(i["id"]), escape(i["subject"]), i["received"], len(i["mime"]),
                escape(i["sender"]),
                "<t:InternetMessageId>%s</t:InternetMessageId>" % escape(i["mid"]) if i["mid"] else "")
            for i in page)
        return env('<m:FindItemResponse><m:ResponseMessages><m:FindItemResponseMessage ResponseClass="Success">'
                   '<m:ResponseCode>NoError</m:ResponseCode>'
                   '<m:RootFolder TotalItemsInView="%d" IncludesLastItemInRange="%s"><t:Items>%s</t:Items>'
                   '</m:RootFolder></m:FindItemResponseMessage></m:ResponseMessages></m:FindItemResponse>'
                   % (len(items), str(last).lower(), parts))

    def op_GetItem(self, xml):
        ids = re.findall(r'<t:ItemId Id="([^"]+)"', xml)
        self.get_item_calls.append(len(ids))
        if self.fault_ids & set(ids):
            raise _Fault()
        by_id = {i["id"]: i for lst in self.items.values() for i in lst}
        parts = ""
        for iid in ids:
            it = by_id.get(iid)
            if it is None or it["error"]:
                parts += ('<m:GetItemResponseMessage ResponseClass="Error"><m:MessageText>secret text</m:MessageText>'
                          '<m:ResponseCode>%s</m:ResponseCode><m:Items/></m:GetItemResponseMessage>'
                          % (it["error"] if it else "ErrorItemNotFound"))
            else:
                parts += ('<m:GetItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
                          '<m:Items><t:Message><t:MimeContent CharacterSet="UTF-8">%s</t:MimeContent>'
                          '<t:ItemId Id=%s/></t:Message></m:Items></m:GetItemResponseMessage>'
                          % (base64.b64encode(it["mime"]).decode(), quoteattr(iid)))
        return env("<m:GetItemResponse><m:ResponseMessages>%s</m:ResponseMessages></m:GetItemResponse>" % parts)


def start(mailbox: FakeMailbox):
    expected = "Basic " + base64.b64encode(
        ("%s:%s" % (mailbox.user, mailbox.password)).encode("utf-8")).decode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", headers=()):
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            if self.headers.get("Authorization") == expected:
                return True
            self._send(401, headers=[("WWW-Authenticate", 'Basic realm="x"')])
            return False

        def do_GET(self):
            if self._authed():
                self._send(200, b"Service")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if not self._authed():
                return
            code, out = mailbox.handle(body.decode("utf-8"))
            self._send(code, out, [("Content-Type", "text/xml; charset=utf-8")])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/EWS/Exchange.asmx" % server.server_address[1]
