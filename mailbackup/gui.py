"""화면: 로그인 → 백업 시작 → 진행률 → 저장 폴더 열기."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, backup, ews, store, textcopy


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, ews.LoginError):
        return ("로그인하지 못했습니다.\n아이디와 비밀번호를 다시 확인해 주세요.\n\n"
                "아이디는 메일 주소 또는 TOAMASINA\\아이디 형식입니다.")
    if isinstance(exc, ews.CertificateProblem):
        return ("메일 서버의 보안 인증서를 확인할 수 없어 안전을 위해 연결을 중단했습니다.\n"
                "업무망에 연결되어 있는지 확인해 주세요.")
    if isinstance(exc, ews.ConnectionProblem):
        return ("메일 서버에 연결할 수 없습니다.\n업무망 랜선이 연결되어 있는지 확인한 뒤 "
                "다시 시도해 주세요.\n\n(지금까지 받은 메일은 저장되어 있고, 다시 시작하면 이어서 받습니다.)")
    if isinstance(exc, ews.UnexpectedServer):
        return "메일 서버가 예상과 다르게 응답해서 안전을 위해 중단했습니다.\n(%s)" % exc
    if isinstance(exc, ews.EwsError):
        if exc.code == "ErrorServerBusy":
            return "메일 서버가 바쁩니다. 잠시 후 다시 시도해 주세요.\n(다시 시작하면 이어서 받습니다.)"
        return "메일 서버가 요청을 거절했습니다.\n(오류 이름: %s)" % exc.code
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "PC의 저장 공간이 부족합니다. 공간을 확보한 뒤 다시 시도해 주세요."
    return "예상하지 못한 문제가 생겼습니다.\n(%s)" % type(exc).__name__


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.base_dir = store.load_base_dir()
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        root.title("메일 백업 %s" % __version__)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        frm = ttk.Frame(root, padding=16)
        frm.grid(sticky="nsew")

        ttk.Label(frm, text="회사 웹메일을 이 PC에 복사합니다. 서버의 메일은 그대로 남습니다.").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Label(frm, text="아이디").grid(row=1, column=0, sticky="w")
        self.user_var = tk.StringVar()
        self.user_entry = ttk.Entry(frm, textvariable=self.user_var, width=42)
        self.user_entry.grid(row=1, column=1, columnspan=2, sticky="we", pady=3)
        ttk.Label(frm, text="메일 주소 또는 TOAMASINA\\아이디", foreground="#666").grid(
            row=2, column=1, columnspan=2, sticky="w")

        ttk.Label(frm, text="비밀번호").grid(row=3, column=0, sticky="w")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(frm, textvariable=self.pass_var, width=42, show="●")
        self.pass_entry.grid(row=3, column=1, columnspan=2, sticky="we", pady=3)
        ttk.Label(frm, text="비밀번호는 저장되지 않습니다. 실행할 때마다 입력합니다.",
                  foreground="#666").grid(row=4, column=1, columnspan=2, sticky="w")

        self.recent_var = tk.BooleanVar(value=False)
        self.days_var = tk.StringVar(value="7")
        opt = ttk.Frame(frm)
        opt.grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.recent_chk = ttk.Checkbutton(opt, text="시험용: 최근", variable=self.recent_var)
        self.recent_chk.pack(side="left")
        self.days_spin = ttk.Spinbox(opt, from_=1, to=3650, width=5, textvariable=self.days_var)
        self.days_spin.pack(side="left", padx=4)
        ttk.Label(opt, text="일 동안의 메일만 받기").pack(side="left")

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="we", pady=(14, 8))
        self.start_btn = ttk.Button(btns, text="백업 시작", command=self.on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="중지", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="저장 폴더 열기", command=self.open_folder).pack(side="right")
        self.text_btn = ttk.Button(btns, text="텍스트 사본만 만들기", command=self.on_text_only)
        self.text_btn.pack(side="right", padx=8)

        self.bar = ttk.Progressbar(frm, length=440, mode="determinate")
        self.bar.grid(row=7, column=0, columnspan=3, sticky="we")
        self.status_var = tk.StringVar(value="아이디와 비밀번호를 입력하고 [백업 시작]을 눌러 주세요.")
        ttk.Label(frm, textvariable=self.status_var, wraplength=440, justify="left").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.detail_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.detail_var, wraplength=440, justify="left",
                  foreground="#444").grid(row=9, column=0, columnspan=3, sticky="w", pady=(2, 0))
        loc = ttk.Frame(frm)
        loc.grid(row=10, column=0, columnspan=3, sticky="we", pady=(10, 0))
        self.dir_btn = ttk.Button(loc, text="저장 위치 바꾸기", command=self.change_folder)
        self.dir_btn.pack(side="right")
        self.dir_var = tk.StringVar(value="저장 위치: %s" % self.base_dir)
        ttk.Label(loc, textvariable=self.dir_var, foreground="#666", wraplength=330,
                  justify="left").pack(side="left")

        self.pass_entry.bind("<Return>", lambda _e: self.on_start())
        self.user_entry.focus_set()
        self.root.after(150, self.poll)

    # ---------------------------------------------------------------- 동작

    def set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for w in (self.user_entry, self.pass_entry, self.recent_chk, self.days_spin, self.start_btn,
                  self.text_btn, self.dir_btn):
            w.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    def on_start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        username = self.user_var.get().strip()
        password = self.pass_var.get()
        if not username or not password:
            messagebox.showinfo("메일 백업", "아이디와 비밀번호를 입력해 주세요.")
            return
        days = None
        if self.recent_var.get():
            try:
                days = int(self.days_var.get())
                if days < 1:
                    raise ValueError
            except ValueError:
                messagebox.showinfo("메일 백업", "'최근 며칠'에는 1 이상의 숫자를 넣어 주세요.")
                return

        self.stop_event.clear()
        self.bar.configure(mode="indeterminate")
        self.bar.start(15)
        self.detail_var.set("")
        self.set_running(True)
        self.worker = threading.Thread(target=self.work, args=(username, password, days), daemon=True)
        self.worker.start()

    def work(self, username: str, password: str, days: int | None) -> None:
        client = None
        try:
            logger = backup.setup_logging(self.base_dir)
            client = ews.EwsClient(username, password, stop_event=self.stop_event)
            prog = backup.run_backup(
                client, self.base_dir, days=days, stop_event=self.stop_event, logger=logger,
                on_progress=lambda p: self.events.put(("progress", backup.Progress(**vars(p)))))
            text = self.make_text(logger)
            self.events.put(("done", (prog, text)))
        except ews.Stopped:
            self.events.put(("stopped", None))
        except Exception as exc:  # 화면에 쉬운 말로 알려 준다
            self.events.put(("error", exc))
        finally:
            if client is not None:
                client.close()

    def make_text(self, logger) -> textcopy.TextResult:
        return textcopy.run_textcopy(
            self.base_dir, stop_event=self.stop_event, logger=logger,
            on_progress=lambda stage, done, total: self.events.put(("text", (stage, done, total))))

    def on_text_only(self) -> None:
        """서버에 연결하지 않고, 이미 받은 메일로 텍스트 사본만 만든다."""
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.bar.configure(mode="indeterminate")
        self.bar.start(15)
        self.detail_var.set("")
        self.set_running(True)
        self.worker = threading.Thread(target=self.work_text_only, daemon=True)
        self.worker.start()

    def work_text_only(self) -> None:
        try:
            logger = backup.setup_logging(self.base_dir)
            self.events.put(("done", (None, self.make_text(logger))))
        except ews.Stopped:
            self.events.put(("stopped", None))
        except Exception as exc:
            self.events.put(("error", exc))

    def change_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="메일을 저장할 폴더를 골라 주세요", initialdir=os.path.dirname(self.base_dir) or None)
        if not chosen:
            return
        chosen = os.path.normpath(chosen)
        if os.path.basename(chosen).lower() != "mailbackup":
            chosen = os.path.join(chosen, "MailBackup")
        if chosen == self.base_dir:
            return
        try:
            os.makedirs(chosen, exist_ok=True)
            store.save_base_dir(chosen)
        except OSError:
            messagebox.showerror("메일 백업", "그 폴더에는 저장할 수 없습니다. 다른 폴더를 골라 주세요.")
            return
        old, self.base_dir = self.base_dir, chosen
        self.dir_var.set("저장 위치: %s" % chosen)
        if os.path.isdir(os.path.join(old, "원본")) and not os.path.isdir(os.path.join(chosen, "원본")):
            messagebox.showinfo(
                "메일 백업",
                "저장 위치를 바꿨습니다.\n\n이미 받은 메일은 옮겨지지 않습니다. 이어서 받으려면 "
                "예전 폴더의 내용(원본·상태·텍스트·첨부 폴더)을 새 폴더로 직접 옮겨 주세요.\n"
                "옮기지 않으면 새 폴더에 처음부터 다시 받습니다.\n\n예전 폴더: %s" % old)

    def on_stop(self) -> None:
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("중지하는 중… 받고 있던 메일까지만 마무리합니다.")

    def poll(self) -> None:
        last_progress = None
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "progress":
                    last_progress = data
                elif kind == "text":
                    last_progress = None
                    self.show_text_progress(*data)
                else:
                    if last_progress is not None:
                        self.show_progress(last_progress)
                        last_progress = None
                    self.finish(kind, data)
        except queue.Empty:
            pass
        if last_progress is not None:
            self.show_progress(last_progress)
        self.root.after(150, self.poll)

    def show_progress(self, p: backup.Progress) -> None:
        if p.stage == "메일을 받는 중…" and p.total > 0:
            if str(self.bar.cget("mode")) != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar.configure(maximum=p.total, value=min(p.checked, p.total))
            pct = int(min(p.checked, p.total) * 100 / p.total)
            self.status_var.set("메일을 받는 중… %d%%  (%s / %s통 확인)" % (
                pct, format(p.checked, ","), format(p.total, ",")))
        else:
            self.status_var.set(p.stage)
        if p.stage == "메일 수를 세는 중…":
            self.detail_var.set("지금까지 %s통" % format(p.total, ","))
        elif p.folder:
            self.detail_var.set("폴더: %s\n새로 저장 %s통 · 이미 받음 %s통 · 실패 %s통" % (
                p.folder, format(p.saved, ","), format(p.skipped, ","), format(p.failed, ",")))

    def show_text_progress(self, stage: str, done: int, total: int) -> None:
        if total > 0:
            if str(self.bar.cget("mode")) != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar.configure(maximum=total, value=done)
            self.status_var.set("텍스트 사본: %s  (%s / %s)" % (stage, format(done, ","), format(total, ",")))
        else:
            self.status_var.set("텍스트 사본: %s" % stage)

    def finish(self, kind: str, data) -> None:
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.set_running(False)
        if kind == "done":
            p, t = data
            self.bar.configure(maximum=1, value=1)
            lines = []
            if p is not None:
                self.status_var.set("백업과 텍스트 사본 만들기가 끝났습니다.")
                msg = "메일: 새로 저장 %s통 · 이미 받아서 건너뜀 %s통" % (
                    format(p.saved, ","), format(p.skipped, ","))
                if p.failed:
                    msg += " · 받지 못함 %s통 (다음 실행 때 다시 시도)" % format(p.failed, ",")
                lines.append(msg)
            else:
                self.status_var.set("텍스트 사본 만들기가 끝났습니다.")
            msg = "텍스트 사본: 대화 %s개 중 %s개를 새로 쓰거나 갱신" % (
                format(t.threads, ","), format(t.written, ","))
            if t.failed:
                msg += " · 읽지 못한 메일 %s통" % format(t.failed, ",")
            lines.append(msg)
            if not t.csv_ok:
                lines.append("대화목록.csv 를 저장하지 못했습니다. 엑셀에서 열려 있으면 닫고 다시 실행해 주세요.")
            self.detail_var.set("\n".join(lines))
        elif kind == "stopped":
            self.status_var.set("중지했습니다. 다시 [백업 시작]을 누르면 이어서 받습니다.")
        elif kind == "error":
            self.status_var.set("백업을 마치지 못했습니다.")
            messagebox.showerror("메일 백업", friendly_error(data))

    def open_folder(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(self.base_dir)  # noqa: S606 - 탐색기로 폴더 열기
        else:
            subprocess.Popen(["xdg-open", self.base_dir])

    def on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askokcancel(
                    "메일 백업", "백업이 진행 중입니다. 중지하고 닫을까요?\n(다음에 이어서 받을 수 있습니다.)"):
                return
            self.stop_event.set()
            self.worker.join(timeout=5)
        self.root.destroy()


def main() -> None:
    try:
        # Windows에 등록된 인증서(회사 인증서 포함)로 서버를 검증한다. 검증을 끄지 않는다.
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
