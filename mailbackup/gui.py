"""화면: 로그인 → 백업 시작 → 진행률 → 저장 폴더 열기."""

from __future__ import annotations

import os
import queue
import shutil
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
    if isinstance(exc, AlreadyRunning):
        return "이 저장 폴더로 이미 다른 메일 백업 창이 작업 중입니다.\n그 창의 작업이 끝난 뒤 다시 시도해 주세요."
    if isinstance(exc, backup.LowDisk):
        return ("PC의 저장 공간이 부족해서 멈췄습니다 (여유 공간 500MB 미만).\n"
                "공간을 확보하거나 [저장 위치 바꾸기]로 다른 드라이브를 고른 뒤 다시 시도해 주세요.\n\n"
                "(지금까지 받은 메일은 저장되어 있고, 다시 시작하면 이어서 받습니다.)")
    if isinstance(exc, FileNotFoundError):
        return "저장 폴더를 찾을 수 없습니다.\n저장 위치의 드라이브가 연결되어 있는지 확인해 주세요."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "PC의 저장 공간이 부족합니다. 공간을 확보한 뒤 다시 시도해 주세요."
    return "예상하지 못한 문제가 생겼습니다.\n(%s)" % type(exc).__name__


class AlreadyRunning(Exception):
    """같은 저장 폴더로 다른 창이 이미 작업 중."""


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.base_dir = store.load_base_dir()
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False  # 작업 시작부터 화면에 결과를 표시할 때까지

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

        recent_only, recent_days = store.load_recent()
        self.recent_var = tk.BooleanVar(value=recent_only)
        self.days_var = tk.StringVar(value=str(recent_days))
        optbox = ttk.Frame(frm)
        optbox.grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))
        opt = ttk.Frame(optbox)
        opt.pack(anchor="w")
        self.recent_chk = ttk.Checkbutton(opt, text="빠른 실행: 최근", variable=self.recent_var)
        self.recent_chk.pack(side="left")
        self.days_spin = ttk.Spinbox(opt, from_=1, to=3650, width=5, textvariable=self.days_var)
        self.days_spin.pack(side="left", padx=4)
        ttk.Label(opt, text="일 동안의 메일만 확인").pack(side="left")
        ttk.Label(optbox, text="체크를 풀면 메일함 전체를 확인합니다. 가끔은 전체로 실행해 주세요.",
                  foreground="#666").pack(anchor="w", pady=(2, 0))

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
        self.root.after(300, self.first_checks)

    def first_checks(self) -> None:
        """시작할 때 한 번: 예전 저장 폴더 옮기기 제안, 클라우드 동기화 폴더 경고."""
        old = store.old_default_base_dir()
        if (not store.has_saved_base_dir() and os.path.isdir(os.path.join(old, "원본"))
                and not os.path.exists(self.base_dir)):
            if messagebox.askyesno(
                    "메일 백업",
                    "기본 저장 위치가 바탕화면으로 바뀌었습니다.\n\n이전에 받은 메일 폴더를 바탕화면으로 옮길까요?\n\n"
                    "예전: %s\n새 위치: %s\n\n[아니요]를 누르면 예전 위치를 계속 사용합니다." % (old, self.base_dir)):
                try:
                    shutil.move(old, self.base_dir)
                except OSError:
                    messagebox.showerror("메일 백업", "폴더를 옮기지 못했습니다. 예전 위치를 계속 사용합니다.")
                    self.set_base_dir(old)
            else:
                self.set_base_dir(old)
        self.warn_if_cloud()

    def set_base_dir(self, path: str) -> None:
        self.base_dir = path
        self.dir_var.set("저장 위치: %s" % path)
        try:
            store.save_base_dir(path)
        except OSError:
            pass

    def warn_if_cloud(self) -> None:
        name = store.cloud_sync_name(self.base_dir)
        if name:
            messagebox.showwarning(
                "메일 백업",
                "지금 저장 위치는 %s 와(과) 자동으로 동기화되는 폴더입니다.\n\n%s\n\n"
                "여기에 저장하면 회사 메일이 인터넷의 개인 클라우드로 올라갑니다.\n"
                "[저장 위치 바꾸기]로 동기화되지 않는 폴더(예: C:\\MailBackup)를 골라 주세요."
                % (name, self.base_dir))

    # ---------------------------------------------------------------- 동작

    def set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for w in (self.user_entry, self.pass_entry, self.recent_chk, self.days_spin, self.start_btn,
                  self.text_btn, self.dir_btn):
            w.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    def confirm_cloud(self) -> bool:
        name = store.cloud_sync_name(self.base_dir)
        if not name:
            return True
        return messagebox.askyesno(
            "메일 백업",
            "저장 위치가 %s 동기화 폴더입니다. 회사 메일이 개인 클라우드로 올라갑니다.\n\n"
            "그래도 이 위치에 저장할까요?\n([아니요]를 누르고 [저장 위치 바꾸기]를 권합니다.)" % name,
            default="no", icon="warning")

    def on_start(self) -> None:
        if self.running:
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

        if not self.confirm_cloud():
            return
        try:  # 다음 실행 때도 같은 선택으로 시작하도록 기억
            if days is not None:
                store.save_settings(recent_only=True, recent_days=days)
            else:
                store.save_settings(recent_only=False)
        except OSError:
            pass
        self.stop_event.clear()
        self.bar.configure(mode="indeterminate")
        self.bar.start(15)
        self.detail_var.set("")
        self.set_running(True)
        self.worker = threading.Thread(target=self.work, args=(username, password, days), daemon=True)
        self.worker.start()

    def work(self, username: str, password: str, days: int | None) -> None:
        client = None
        lock = None
        try:
            lock = self.lock()
            logger = backup.setup_logging(self.base_dir)
            client = ews.EwsClient(username, password, stop_event=self.stop_event)
            prog = backup.run_backup(
                client, self.base_dir, days=days, stop_event=self.stop_event, logger=logger,
                on_progress=lambda p: self.events.put(("progress", backup.Progress(**vars(p)))))
            try:
                text = self.make_text(logger)
            except ews.Stopped:
                raise
            except Exception as exc:  # 백업은 끝났고 텍스트 사본만 실패
                logger.info("텍스트 사본 실패: %s", type(exc).__name__)
                text = exc
            self.events.put(("done", (prog, text)))
        except ews.Stopped:
            self.events.put(("stopped", None))
        except Exception as exc:  # 화면에 쉬운 말로 알려 준다
            self.events.put(("error", exc))
        finally:
            if client is not None:
                client.close()
            if lock is not None:
                lock.close()

    def lock(self):
        os.makedirs(self.base_dir, exist_ok=True)
        handle = store.acquire_lock(self.base_dir)
        if handle is None:
            raise AlreadyRunning()
        return handle

    def make_text(self, logger) -> textcopy.TextResult:
        backup.check_disk(self.base_dir)
        return textcopy.run_textcopy(
            self.base_dir, stop_event=self.stop_event, logger=logger,
            on_progress=lambda stage, done, total: self.events.put(("text", (stage, done, total))))

    def on_text_only(self) -> None:
        """서버에 연결하지 않고, 이미 받은 메일로 텍스트 사본만 만든다."""
        if self.running:
            return
        self.stop_event.clear()
        self.bar.configure(mode="indeterminate")
        self.bar.start(15)
        self.detail_var.set("")
        self.set_running(True)
        self.worker = threading.Thread(target=self.work_text_only, daemon=True)
        self.worker.start()

    def work_text_only(self) -> None:
        lock = None
        try:
            lock = self.lock()
            logger = backup.setup_logging(self.base_dir)
            self.events.put(("done", (None, self.make_text(logger))))
        except ews.Stopped:
            self.events.put(("stopped", None))
        except Exception as exc:
            self.events.put(("error", exc))
        finally:
            if lock is not None:
                lock.close()

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
        name = store.cloud_sync_name(chosen)
        if name and not messagebox.askyesno(
                "메일 백업",
                "고른 폴더는 %s 와(과) 자동으로 동기화되는 폴더입니다.\n"
                "여기에 저장하면 회사 메일이 개인 클라우드로 올라갑니다.\n\n그래도 이 폴더를 쓸까요?" % name,
                default="no", icon="warning"):
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
                "예전 폴더의 내용(원본·상태·텍스트 폴더와 대화목록.csv)을 새 폴더로 직접 옮겨 주세요.\n"
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
        self.pass_var.set("")  # 비밀번호는 작업이 끝나면 화면에서도 지운다
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
            if isinstance(t, Exception):
                self.status_var.set("메일 백업은 끝났지만 텍스트 사본을 만들지 못했습니다.")
                lines.append("텍스트 사본: " + friendly_error(t).split("\n")[0]
                             + " [텍스트 사본만 만들기]로 다시 시도할 수 있습니다.")
            elif t.no_source:
                lines.append("텍스트 사본: 받아 둔 메일이 없어 만들지 않았습니다.")
            else:
                msg = "텍스트 사본: 대화 %s개 중 %s개를 새로 쓰거나 갱신" % (
                    format(t.threads, ","), format(t.written, ","))
                if t.failed:
                    msg += " · 처리하지 못함 %s건 (다음 실행 때 다시 시도)" % format(t.failed, ",")
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
        try:
            os.makedirs(self.base_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("메일 백업", friendly_error(exc))
            return
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
