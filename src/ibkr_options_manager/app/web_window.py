from __future__ import annotations

# ruff: noqa: E501
from collections.abc import Callable
from threading import Thread
from time import monotonic, sleep

import uvicorn
from PySide6.QtCore import QUrl
from PySide6.QtGui import QCloseEvent
from PySide6.QtWebEngineCore import (
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMainWindow

from ..execution import PaperExecutionService
from .view_model import PlannerViewModel
from .web import StarUIWorkbench


class _LoopbackOnlyRequestInterceptor(QWebEngineUrlRequestInterceptor):
    """Reject every browser request except the process-local StarUI surface."""

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:
        url = info.requestUrl()
        if url.scheme() != "http" or url.host() != "127.0.0.1":
            info.block(True)


class StarUIPlannerWindow(QMainWindow):
    """Embedded, loopback-only StarUI renderer for the paper workbench."""

    def __init__(
        self,
        view_model: PlannerViewModel,
        *,
        initial_account: str = "",
        initial_con_id: int | None = None,
        demo_mode: bool = False,
        paper_execution: PaperExecutionService | None = None,
    ) -> None:
        super().__init__()
        self._demo_mode = demo_mode
        self._surface = StarUIWorkbench(
            view_model,
            initial_account=initial_account,
            initial_con_id=initial_con_id,
            demo_mode=demo_mode,
            paper_execution=paper_execution,
        )
        self._server, self._thread, port = _start_local_server(self._surface.app)
        self._url = QUrl(f"http://127.0.0.1:{port}{self._surface.path}")
        self.setWindowTitle("IBKR Options Manager — Paper OCA manager")
        self.resize(1500, 920)
        self.setMinimumSize(1120, 720)
        self._view = QWebEngineView(self)
        self._view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        self._interceptor = _LoopbackOnlyRequestInterceptor(self)
        self._view.page().profile().setUrlRequestInterceptor(self._interceptor)
        self.setCentralWidget(self._view)
        self._closed = False

    def load_demo_data(self) -> None:
        self._surface.load_demo_data()
        self._view.setUrl(self._url)

    def refresh_on_launch(self) -> None:
        """Begin the in-page connection flow without delaying initial paint."""
        self._surface.start_launch_refresh()
        # Do not navigate until the surface is marked as connecting. Otherwise
        # the first page can render while the state is still idle, leaving it
        # with no dialog or polling script to observe the background result.
        self._view.setUrl(self._url)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closed = True
        self._server.should_exit = True
        self._thread.join(timeout=2)
        super().closeEvent(event)


def _start_local_server(app: Callable[..., object]) -> tuple[uvicorn.Server, Thread, int]:
    """Start StarHTML on loopback only and wait until the port is serving."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = Thread(target=server.run, name="starui-workbench", daemon=True)
    thread.start()
    deadline = monotonic() + 3
    while not server.started and thread.is_alive() and monotonic() < deadline:
        sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=1)
        raise RuntimeError("The local StarUI workbench did not start.")
    return server, thread, port


__all__ = ["StarUIPlannerWindow"]
