import pytest

tk = pytest.importorskip("tkinter")


def test_window_opens_and_closes():
    from mailbackup import gui

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면이 없는 환경")
    app = gui.App(root)
    root.update()
    assert app.start_btn.instate(["!disabled"])
    assert app.pass_entry.cget("show")  # 비밀번호는 가려서 표시
    root.destroy()


def test_error_messages_are_korean():
    from mailbackup import ews, gui

    for exc in (ews.LoginError(), ews.CertificateProblem(), ews.ConnectionProblem(),
                ews.UnexpectedServer("HTTP 302"), ews.EwsError("ErrorServerBusy"),
                ews.EwsError("ErrorAccessDenied"), ValueError("x")):
        assert gui.friendly_error(exc)
