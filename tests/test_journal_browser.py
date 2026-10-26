import json

import pytest
from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWebEngineWidgets import QWebEngineView
from starlette.testclient import TestClient

from ibkr_options_manager.app.web_window import _start_local_server
from support.qt import wait_for_javascript
from support.workbench import _demo_workbench

pytestmark = pytest.mark.gui


def test_journal_views_render_at_desktop_and_minimum_size(tmp_path):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.show()
    screenshots = tmp_path / "captures"
    screenshots.mkdir(parents=True, exist_ok=True)
    selected = next(
        r
        for r in workbench._journal_rows()
        if r.contract.con_id == workbench._selected_con_id
    )
    analysis_widths = {}
    analysis_header_bounds = {}
    management_header_bounds = {}
    try:
        for name, action in (
            ("dashboard", {"action": "show-dashboard"}),
            ("analysis", {"action": "open-trade", "trade_id": selected.row_id}),
            ("management", {"action": "show-management"}),
            (
                "analysis-qqq",
                {
                    "action": "open-trade",
                    "trade_id": next(
                        row.row_id
                        for row in workbench._journal_rows()
                        if row.contract.trading_class == "QQQ"
                    ),
                },
            ),
        ):
            client.post(workbench.path + "action", data=action)
            for width in (1500, 1120, 2560):
                view.resize(width, 920)
                loop = QEventLoop()
                observations = []

                def loaded(ok, observations=observations, loop=loop):
                    assert ok

                    def settled(value):
                        view.page().runJavaScript(
                            """JSON.stringify({
                            overflow: document.documentElement.scrollWidth > innerWidth,
                            chart: Array.from(document.querySelectorAll(
                              '.journal-equity-chart,.journal-price-chart'
                            )).every(e => e.getBoundingClientRect().width > 0),
                            dashboard: !!document.querySelector(
                              '[data-journal-dashboard]'),
                            analysis: !!document.querySelector(
                              '[data-position-analysis]'),
                            analysisSidebar: !!document.querySelector(
                              '.journal-analysis-sidebar'),
                            workspaceWidth: document.querySelector(
                              '.journal-analysis,.workspace-content')
                              ?.getBoundingClientRect().width,
                            analysisMetrics: document.querySelectorAll(
                              '.journal-analysis-sidebar .journal-metric').length,
                            headerBounds: Array.from(document.querySelectorAll(
                              '.journal-analysis .contract-header-facts, '
                              + '.journal-management .contract-header-facts, '
                              + '.journal-analysis .journal-position-tabs, '
                              + '.journal-management .journal-position-tabs'
                            )).map(e => {
                              const {x, y, width, height} = e.getBoundingClientRect();
                              return {x, y, width, height};
                            }),
                            positionHierarchy: (() => {
                              const scope = document.querySelector(
                                '.journal-analysis,.journal-management');
                              if (!scope) return true;
                              const title = scope.querySelector('h1');
                              const facts = scope.querySelector(
                                '.contract-header-facts');
                              const tabs = scope.querySelector(
                                '.journal-position-tabs');
                              const actions = scope.querySelector(
                                '.position-management-actions');
                              const titleRow = title?.closest('.position-title-row');
                              const analysis = !!document.querySelector(
                                '[data-position-analysis]');
                              const box = element => element.getBoundingClientRect();
                              return !!(title && facts && tabs)
                                && box(title).bottom <= box(facts).top
                                && box(facts).bottom <= box(tabs).top
                                && (analysis ? !actions :
                                    titleRow?.contains(actions)
                                    && box(actions).bottom <= box(facts).top
                                    && Math.abs(box(actions).right
                                      - box(facts).right) < 2);
                            })()
                        })""",
                            lambda result: (
                                observations.append(json.loads(result)),
                                loop.quit(),
                            ),
                        )

                    wait_for_javascript(
                        view,
                        "document.readyState === 'complete' && "
                        "!!document.querySelector('.journal-app-navigation')",
                        settled,
                    )

                view.loadFinished.connect(loaded)
                view.setUrl(
                    QUrl(
                        f"http://127.0.0.1:{port}{workbench.path}?capture={name}-{width}"
                    )
                )
                QTimer.singleShot(8000, loop.quit)
                loop.exec()
                view.loadFinished.disconnect(loaded)
                assert observations
                assert not observations[0]["overflow"]
                assert observations[0]["chart"]
                assert observations[0]["dashboard"] == (name == "dashboard")
                assert observations[0]["analysis"] == name.startswith("analysis")
                assert observations[0]["positionHierarchy"]
                assert observations[0]["analysisSidebar"] == name.startswith("analysis")
                if name.startswith("analysis"):
                    analysis_widths[width] = observations[0]["workspaceWidth"]
                    if name == "analysis":
                        analysis_header_bounds[width] = observations[0]["headerBounds"]
                    assert observations[0]["analysisMetrics"] == 5
                elif name == "management":
                    assert observations[0]["workspaceWidth"] == analysis_widths[width]
                    management_header_bounds[width] = observations[0]["headerBounds"]
                QTest.qWait(150)  # allow the rendered compositor frame to reach grab()
                view.grab().save(str(screenshots / f"{name}-{width}.png"))
        # Compare the same open position before the later closed-trade capture.
        assert management_header_bounds == analysis_header_bounds
    finally:
        view.close()
        view.deleteLater()
        server.should_exit = True
        thread.join(timeout=2)
        workbench.close()


def test_active_position_opens_management(tmp_path):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "show-dashboard"})
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    loop = QEventLoop()
    seen = []
    try:

        def loaded(ok):
            assert ok
            if workbench._main_view == "dashboard":
                view.page().runJavaScript(
                    "document.querySelector('[data-active-positions] button').click()"
                )
            else:
                wait_for_javascript(
                    view,
                    "!!document.querySelector('.journal-management') && "
                    "!document.querySelector('[data-active-positions]')",
                    lambda result: (seen.append(result), loop.quit()),
                )

        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(12000, loop.quit)
        loop.exec()
        assert seen == [True]
        assert workbench._main_view == "positions"
        assert workbench._position_view == "management"
        assert workbench._selected_con_id == workbench._state.positions[0].con_id
    finally:
        view.close()
        view.deleteLater()
        server.should_exit = True
        thread.join(timeout=2)
        workbench.close()


def test_equity_points_pointer_keyboard_and_resize(tmp_path):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    client = TestClient(workbench.app)
    client.post(workbench.path + "action", data={"action": "show-dashboard"})
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    loop = QEventLoop()
    seen = []
    try:

        def loaded(ok):
            assert ok

            def ready(value):
                if not value:
                    seen.append(False)
                    loop.quit()
                    return
                view.page().runJavaScript(
                    """(() => {
                  const root = document.querySelector('[data-interactive-equity]');
                  const svg = root.querySelector('svg');
                  const points = [...root.querySelectorAll('[data-equity-point]')];
                  const box = svg.getBoundingClientRect();
                  svg.dispatchEvent(new PointerEvent('pointermove', {
                    clientX:box.right-12, clientY:box.top+100, bubbles:true
                  }));
                  const pointer = root.querySelector('[data-equity-tooltip]')
                    .textContent.includes('788.50');
                  points[points.length-1].focus();
                  points[points.length-1].dispatchEvent(new KeyboardEvent(
                    'keydown', {key:'ArrowLeft', bubbles:true}));
                  const keyboard = document.activeElement === points[points.length-2]
                    && points[points.length-2].getAttribute('aria-pressed') === 'true';
                  const tooltip = root.querySelector('[data-equity-tooltip]');
                  const tipBox = tooltip.getBoundingClientRect();
                  const rootBox = root.getBoundingClientRect();
                  const dayValue = tooltip.querySelector('[data-equity-day-value]');
                  const totalValue = tooltip.querySelector('[data-equity-total-value]');
                  const valueAlignment = Math.abs(dayValue.getBoundingClientRect().right
                    - totalValue.getBoundingClientRect().right) < 1;
                  const label = tooltip.querySelector('.journal-tooltip-values')
                    .children[2].getBoundingClientRect();
                  const spacing = totalValue.getBoundingClientRect().left
                    - label.right >= 30;
                  const positive = dayValue.classList.contains('journal-positive')
                    && dayValue.textContent.startsWith('+');
                  const negativePoint = points.find(p =>
                    p.dataset.dayTone === 'journal-negative');
                  negativePoint.focus();
                  const negative = dayValue.classList.contains('journal-negative')
                    && dayValue.textContent.startsWith('-');
                  points[points.length-2].focus();
                  const section = root.closest('.journal-equity-section');
                  const fullHeight = box.height > 300 && Math.abs(rootBox.bottom -
                    (section.getBoundingClientRect().bottom -
                    parseFloat(getComputedStyle(section).paddingBottom))) < 2;
                  const tooltipFits = !tooltip.hidden && tipBox.left >= rootBox.left
                    && tipBox.right <= rootBox.right + 1;
                  points[points.length-2].dispatchEvent(new KeyboardEvent(
                    'keydown', {key:'Escape', bubbles:true}));
                  const dismissed = tooltip.hidden;
                  points[points.length-2].dispatchEvent(new MouseEvent('click'));
                  const reopened = !tooltip.hidden;
                  const day = points[points.length-2].dataset.day;
                  root.querySelector('[data-day-trades]').click();
                  const history = [...document.querySelectorAll('[data-history-day]')];
                  const visible = history.filter(row => !row.hidden);
                  const filtered = visible.length > 0 && visible.every(row =>
                    row.dataset.historyDay === day &&
                    row.dataset.historyCurrency === root.dataset.currency);
                  const clear = document.querySelector('[data-clear-history-day]');
                  const filterShown = !clear.hidden;
                  clear.click();
                  const cleared = clear.hidden && history.every(row => !row.hidden);
                  points[points.length-2].focus();
                  const last = points[points.length-1];
                  const width = Math.abs(Number(last.getAttribute('cx'))
                    - (box.width-12)) < 1;
                  return JSON.stringify({pointer, keyboard, width, tooltipFits,
                    dismissed, reopened, filtered, filterShown, cleared,
                    valueAlignment, spacing, positive, negative, fullHeight,
                    count:points.length,
                    text:root.querySelector('[data-equity-tooltip]').textContent});
                })()""",
                    lambda result: (seen.append(json.loads(result)), loop.quit()),
                )

            wait_for_javascript(
                view,
                "document.querySelector('[data-equity-point]') && "
                "Math.abs(Number(document.querySelector('.journal-equity-chart')"
                ".getAttribute('viewBox').split(' ')[2]) - "
                "document.querySelector('.journal-equity-chart')"
                ".getBoundingClientRect().width) < 1",
                ready,
                timeout=5,
            )

        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(10000, loop.quit)
        loop.exec()
        assert seen and seen[0]
        assert seen[0]["pointer"] and seen[0]["keyboard"] and seen[0]["width"]
        assert all(
            seen[0][key]
            for key in (
                "valueAlignment",
                "spacing",
                "positive",
                "negative",
                "fullHeight",
            )
        )
        assert seen[0]["tooltipFits"] and seen[0]["dismissed"] and seen[0]["reopened"]
        assert seen[0]["filtered"] and seen[0]["filterShown"] and seen[0]["cleared"]
        assert seen[0]["count"] == len(
            {
                r.exit_at // 86400
                for r in workbench._journal_rows()
                if r.exit_at is not None and r.realized_pnl is not None
            }
        )
        view.resize(1120, 920)
        resize_loop = QEventLoop()
        resize_seen = []
        wait_for_javascript(
            view,
            "Math.abs(Number([...document.querySelectorAll('[data-equity-point]')]"
            ".at(-1).getAttribute('cx')) - (document.querySelector("
            "'.journal-equity-chart').getBoundingClientRect().width-12)) < 1",
            lambda value: (resize_seen.append(value), resize_loop.quit()),
            timeout=3,
        )
        QTimer.singleShot(4000, resize_loop.quit)
        resize_loop.exec()
        assert resize_seen == [True]
        QTest.qWait(150)
        view.grab().save(str(tmp_path / "equity-point-selected.png"))
    finally:
        view.close()
        view.deleteLater()
        server.should_exit = True
        thread.join(timeout=2)
        workbench.close()


def test_price_path_points_pointer_keyboard_and_resize(tmp_path):
    workbench = _demo_workbench()
    workbench.load_demo_data()
    row = next(
        r
        for r in workbench._journal_rows()
        if r.contract.con_id == workbench._selected_con_id
    )
    client = TestClient(workbench.app)
    client.post(
        workbench.path + "action",
        data={"action": "open-trade", "trade_id": row.row_id},
    )
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    loop = QEventLoop()
    seen = []
    try:

        def loaded(ok):
            assert ok

            def ready(value):
                assert value
                view.page().runJavaScript(
                    """(() => {
                      const root = document.querySelector('[data-interactive-price]');
                      const svg = root.querySelector('svg');
                      const points = [...root.querySelectorAll('[data-price-point]')];
                      const tooltip = root.querySelector('[data-price-tooltip]');
                      const box = svg.getBoundingClientRect();
                      const last = points.at(-1);
                      svg.dispatchEvent(new PointerEvent('pointermove', {
                        clientX:box.left+Number(last.getAttribute('cx')),
                        clientY:box.top+Number(last.getAttribute('cy')), bubbles:true
                      }));
                      const pointer = !tooltip.hidden &&
                        tooltip.querySelector('[data-price-close]').textContent
                          === last.dataset.close &&
                        tooltip.querySelector('[data-price-date]').textContent
                          === last.dataset.dateLabel;
                      svg.dispatchEvent(new PointerEvent('pointermove', {
                        clientX:box.right-12,
                        clientY:box.top+Number(last.getAttribute('cy'))+35,
                        bubbles:true
                      }));
                      const outside=tooltip.hidden;
                      const a=points[60], b=points[61];
                      svg.dispatchEvent(new PointerEvent('pointermove', {
                        clientX:box.left+(Number(a.getAttribute('cx'))+
                          Number(b.getAttribute('cx')))/2,
                        clientY:box.top+(Number(a.getAttribute('cy'))+
                          Number(b.getAttribute('cy')))/2+6, bubbles:true
                      }));
                      const corridor=!tooltip.hidden;
                      const drag=root.querySelector('.journal-scenario-hit');
                      drag.dispatchEvent(new PointerEvent('pointermove', {
                        clientX:box.left+100,
                        clientY:box.top+Number(drag.getAttribute('y1')),
                        bubbles:true
                      }));
                      const dragPriority=tooltip.hidden;
                      points[0].focus();
                      points[0].dispatchEvent(new KeyboardEvent('keydown', {
                        key:'ArrowRight', bubbles:true
                      }));
                      const keyboard = document.activeElement === points[1]
                        && points[1].getAttribute('aria-pressed') === 'true';
                      points[1].dispatchEvent(new KeyboardEvent('keydown', {
                        key:'End', bubbles:true
                      }));
                      const end = document.activeElement === last;
                      const values = ['low', 'high', 'return'].every(key =>
                        tooltip.querySelector(`[data-price-${key}]`).textContent
                          === last.dataset[key]);
                      const tipBox = tooltip.getBoundingClientRect();
                      const rootBox = root.getBoundingClientRect();
                      const tooltipFits = tipBox.left >= rootBox.left &&
                        tipBox.right <= rootBox.right + 1;
                      last.dispatchEvent(new KeyboardEvent('keydown', {
                        key:'Escape', bubbles:true
                      }));
                      const dismissed = tooltip.hidden;
                      last.dispatchEvent(new MouseEvent('click'));
                      const reopened = !tooltip.hidden;
                      const fullWidth = Math.abs(box.width - rootBox.width) < 1 &&
                        Math.abs(Number(last.getAttribute('cx'))-(box.width-12)) < 1
                        && Number(points[0].getAttribute('cx')) === 12;
                      return JSON.stringify({pointer, outside, corridor, dragPriority,
                        keyboard, end, values,
                        tooltipFits, dismissed, reopened, fullWidth,
                        count:points.length});
                    })()""",
                    lambda result: (seen.append(json.loads(result)), loop.quit()),
                )

            wait_for_javascript(
                view,
                "!!document.querySelector('[data-price-point]') && "
                "document.querySelector('[data-price-point]').getAttribute('cx')"
                " === '12'",
                ready,
            )

        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(8000, loop.quit)
        loop.exec()
        assert seen and all(v for k, v in seen[0].items() if k != "count")
        assert seen[0]["count"] == len(row.analysis.bars)
        view.resize(1120, 920)
        resize_loop = QEventLoop()
        resized = []
        wait_for_javascript(
            view,
            "Math.abs(Number([...document.querySelectorAll('[data-price-point]')]"
            ".at(-1).getAttribute('cx')) - (document.querySelector("
            "'.journal-price-chart').getBoundingClientRect().width-12)) < 1",
            lambda value: (resized.append(value), resize_loop.quit()),
            timeout=3,
        )
        QTimer.singleShot(4000, resize_loop.quit)
        resize_loop.exec()
        assert resized == [True]
        QTest.qWait(150)
        view.grab().save(str(tmp_path / "price-point-selected.png"))
    finally:
        view.close()
        view.deleteLater()
        server.should_exit = True
        thread.join(timeout=2)
        workbench.close()


def test_price_path_native_hover_shows_only_inspected_dot(tmp_path):
    from PySide6.QtCore import QPoint

    workbench = _demo_workbench()
    workbench.load_demo_data()
    row = next(
        r
        for r in workbench._journal_rows()
        if r.contract.con_id == workbench._selected_con_id
    )
    TestClient(workbench.app).post(
        workbench.path + "action",
        data={"action": "open-trade", "trade_id": row.row_id},
    )
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.resize(1500, 920)
    view.show()
    loop = QEventLoop()
    seen = []
    try:

        def loaded(ok):
            assert ok

            def ready(value):
                assert value
                QTest.mouseMove(view.focusProxy(), QPoint(50, 50))
                QTest.qWait(50)
                view.page().runJavaScript(
                    """(() => {
                      const svg = document.querySelector('.journal-price-chart');
                      const box = svg.getBoundingClientRect();
                      const visible = [...svg.querySelectorAll('[data-price-point]')]
                        .filter(p => Number(getComputedStyle(p).opacity) > 0).length;
                      const point=svg.querySelectorAll('[data-price-point]')[60];
                      return JSON.stringify({
                        x:box.left+Number(point.getAttribute('cx')),
                        y:box.top+Number(point.getAttribute('cy')), visible});
                    })()""",
                    lambda result: (seen.append(json.loads(result)), loop.quit()),
                )

            wait_for_javascript(
                view, "!!document.querySelector('[data-price-point]')", ready
            )

        view.loadFinished.connect(loaded)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(8000, loop.quit)
        loop.exec()
        assert seen and seen[0]["visible"] == 0
        # Send actual Qt mouse moves to Chromium's input widget without clicking.
        QTest.mouseMove(view.focusProxy(), QPoint(50, 50))
        QTest.mouseMove(view.focusProxy(), QPoint(int(seen[0]["x"]), int(seen[0]["y"])))
        hover_loop = QEventLoop()
        hovered = []
        wait_for_javascript(
            view,
            "!document.querySelector('[data-price-tooltip]').hidden && "
            "[...document.querySelectorAll('[data-price-point]')]"
            ".filter(p => Number(getComputedStyle(p).opacity) > 0).length === 1",
            lambda value: (hovered.append(value), hover_loop.quit()),
            timeout=3,
        )
        QTimer.singleShot(4000, hover_loop.quit)
        hover_loop.exec()
        assert hovered == [True]
        QTest.qWait(150)
        view.grab().save(str(tmp_path / "native-hover.png"))
        # Also cover renderers delivering ordinary mouse events on the container.
        mouse_loop = QEventLoop()
        fallback = []
        view.page().runJavaScript(
            """(() => {
              const root = document.querySelector('[data-interactive-price]');
              const box = root.querySelector('svg').getBoundingClientRect();
              const last = [...root.querySelectorAll('[data-price-point]')].at(-1);
              root.dispatchEvent(new MouseEvent('mousemove', {
                clientX:box.left+Number(last.getAttribute('cx')),
                clientY:box.top+Number(last.getAttribute('cy')), bubbles:true
              }));
              return last.getAttribute('aria-pressed') === 'true';
            })()""",
            lambda value: (fallback.append(value), mouse_loop.quit()),
        )
        QTimer.singleShot(2000, mouse_loop.quit)
        mouse_loop.exec()
        assert fallback == [True]
        QTest.mouseMove(view.focusProxy(), QPoint(50, 50))
        leave_loop = QEventLoop()
        left = []
        wait_for_javascript(
            view,
            "document.querySelector('[data-price-tooltip]').hidden",
            lambda value: (left.append(value), leave_loop.quit()),
            timeout=3,
        )
        QTimer.singleShot(4000, leave_loop.quit)
        leave_loop.exec()
        assert left == [True]
    finally:
        view.close()
        view.deleteLater()
        server.should_exit = True
        thread.join(timeout=2)
        workbench.close()


def test_analysis_scenario_edit_drag_cancel_and_close_labels(tmp_path):
    from PySide6.QtCore import QPoint, Qt

    workbench = _demo_workbench()
    workbench.load_demo_data()
    original_layers = workbench._current_layers()
    row = next(
        r
        for r in workbench._journal_rows()
        if r.contract.con_id == workbench._selected_con_id
    )
    TestClient(workbench.app).post(
        workbench.path + "action",
        data={
            "action": "open-trade",
            "trade_id": row.row_id,
        },
    )
    server, thread, port = _start_local_server(workbench.app)
    view = QWebEngineView()
    view.resize(1120, 920)
    view.show()

    def js(source):
        loop = QEventLoop()
        values = []
        view.page().runJavaScript(
            source, lambda value: (values.append(value), loop.quit())
        )
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        assert values
        return values[0]

    try:
        loaded = QEventLoop()
        view.loadFinished.connect(loaded.quit)
        view.setUrl(QUrl(f"http://127.0.0.1:{port}{workbench.path}"))
        QTimer.singleShot(8000, loaded.quit)
        loaded.exec()
        assert js("!!document.querySelector('[data-scenario-input]')")
        result = json.loads(
            js("""(() => {
          const root=document.querySelector('[data-interactive-price]');
          const input=root.querySelector('[data-scenario-input="stop"]');
          const visibleTouches=[...root.querySelectorAll('[data-scenario-touch]')]
            .filter(m=>m.style.display!=='none').every(mark=>{
              const box=mark.getBoundingClientRect();
              return document.elementFromPoint(box.left+box.width/2,
                box.top+box.height/2)?.closest('[data-scenario-touch]')===mark;
            });
          input.focus(); input.value='50'; input.dispatchEvent(new Event('input'));
          input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
          const changed=root.querySelector('[data-scenario-price=stop]').textContent;
          const hiddenTouch=root.querySelector('[data-scenario-touch=stop]')
            .style.display==='none';
          input.focus(); input.value='10'; input.dispatchEvent(new Event('input'));
          input.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
          const cancelled=input.value==='50';
          input.focus(); input.value='100'; input.dispatchEvent(new Event('input'));
          const invalid=input.getAttribute('aria-invalid')==='true'
            && !root.querySelector('[data-scenario-error]').hidden;
          input.blur();
          const restored=input.value==='50' &&
            root.querySelector('[data-scenario-error]').hidden;
          const local=!input.name && !input.closest('form');
          const touch=root.querySelector('[data-scenario-touch=target]');
          touch.focus();
          const markerContext=!root.querySelector('[data-price-tooltip]').hidden &&
            root.querySelector('[data-price-context]').textContent
              .includes('not a confirmed fill');
          touch.blur();
          return JSON.stringify({changed,visibleTouches,hiddenTouch,cancelled,invalid,
            restored,local,markerContext});
        })()""")
        )
        assert result.pop("changed") == f"${row.entry_price / 2:.2f}"
        assert all(result.values())
        coordinates = json.loads(
            js("""(() => {
          const line=document.querySelector(
            '.journal-scenario-hit[data-scenario-drag="stop"]');
          const svg=line.ownerSVGElement.getBoundingClientRect();
          return JSON.stringify({x:svg.left+70,
            y:svg.top+Number(line.getAttribute('y1'))});
        })()""")
        )
        js("document.querySelector('[data-scenario-input=stop]').focus()")
        start = QPoint(round(coordinates["x"]), round(coordinates["y"]))
        end = start - QPoint(0, 28)
        QTest.mouseMove(view.focusProxy(), start)
        QTest.mousePress(view.focusProxy(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.focusProxy(), end)
        QTest.qWait(60)
        QTest.mouseRelease(view.focusProxy(), Qt.MouseButton.LeftButton, pos=end)
        QTest.qWait(60)
        assert js(
            "Number(document.querySelector('[data-scenario-input=stop]').value) < 50"
        )
        assert js(
            "!document.querySelector('[data-interactive-price]').classList.contains('is-dragging')"
        )
        # A cancelled drag restores the original level, with no server action.
        before = js(
            "Number(document.querySelector('[data-scenario-input=stop]').value)"
        )
        coordinates = json.loads(
            js("""(() => {
          const line=document.querySelector(
            '.journal-scenario-hit[data-scenario-drag="stop"]');
          const svg=line.ownerSVGElement.getBoundingClientRect();
          return JSON.stringify({x:svg.left+70,
            y:svg.top+Number(line.getAttribute('y1'))});
        })()""")
        )
        start = QPoint(round(coordinates["x"]), round(coordinates["y"]))
        QTest.mousePress(view.focusProxy(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.focusProxy(), start - QPoint(0, 20))
        QTest.qWait(60)
        QTest.keyClick(view.focusProxy(), Qt.Key.Key_Escape)
        QTest.qWait(50)
        QTest.mouseRelease(
            view.focusProxy(), Qt.MouseButton.LeftButton, pos=start - QPoint(0, 20)
        )
        assert (
            js("Number(document.querySelector('[data-scenario-input=stop]').value)")
            == before
        )
        assert js("""(() => {
          const grip=document.querySelector('[data-scenario-grip=stop]');
          const input=document.querySelector('[data-scenario-input=stop]');
          const before=Number(input.value);
          grip.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true}));
          return Number(input.value)<before;
        })()""")
        assert js("""(() => {
          ['stop','target'].forEach(kind=>{
            const input=document.querySelector(`[data-scenario-input=${kind}]`);
            input.value='.1'; input.dispatchEvent(new Event('input'));
          });
          const a=document.querySelector('[data-scenario-drag=stop]')
            .getBoundingClientRect();
          const b=document.querySelector('[data-scenario-drag=target]')
            .getBoundingClientRect();
          return b.bottom<=a.top && a.right<=innerWidth && b.right<=innerWidth;
        })()""")
        QTest.qWait(100)
        view.grab().save(str(tmp_path / "nearby-levels.png"))
        assert workbench._journal_stop == 25 and workbench._journal_target == 20
        assert workbench._current_layers() == original_layers
    finally:
        view.close()
        view.deleteLater()
        server.should_exit = True
        thread.join(timeout=2)
        workbench.close()


def test_price_tooltip_does_not_bridge_missing_minutes():
    from dataclasses import replace

    from starhtml import to_xml

    from ibkr_options_manager.app.web.journal_view import _price_chart

    workbench = _demo_workbench()
    workbench.load_demo_data()
    row = next(r for r in workbench._journal_rows() if r.analysis)
    bars = row.analysis.bars
    row = replace(
        row, analysis=replace(row.analysis, bars=(bars[0], bars[1], bars[-2], bars[-1]))
    )
    view = QWebEngineView()
    view.resize(900, 600)
    view.show()
    loop = QEventLoop()
    seen = []
    try:

        def loaded(ok):
            assert ok

            def ready(value):
                assert value
                view.page().runJavaScript(
                    """(() => {
                      const root=document.querySelector('[data-interactive-price]');
                      const svg=root.querySelector('svg');
                      const points=[...svg.querySelectorAll('[data-price-point]')];
                      const tooltip=root.querySelector('[data-price-tooltip]');
                      const box=svg.getBoundingClientRect();
                      const a=points[1],b=points[2];
                      svg.dispatchEvent(new PointerEvent('pointermove', {
                        clientX:box.left+(Number(a.getAttribute('cx'))+
                          Number(b.getAttribute('cx')))/2,
                        clientY:box.top+(Number(a.getAttribute('cy'))+
                          Number(b.getAttribute('cy')))/2, bubbles:true
                      }));
                      return tooltip.hidden;
                    })()""",
                    lambda result: (seen.append(result), loop.quit()),
                )

            wait_for_javascript(
                view, "!!document.querySelector('[data-price-point]')", ready
            )

        view.loadFinished.connect(loaded)
        view.setHtml(
            "<style>.journal-price-chart{width:100%;height:300px}</style>"
            + to_xml(_price_chart(row))
        )
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        assert seen == [True]
    finally:
        view.close()
        view.deleteLater()
        workbench.close()


def test_scenario_touch_markers_use_ranges_first_touch_and_uncertainty():
    from dataclasses import replace
    from decimal import Decimal

    from starhtml import to_xml

    from ibkr_options_manager.app.web.journal_view import _price_chart

    workbench = _demo_workbench()
    workbench.load_demo_data()
    row = next(r for r in workbench._journal_rows() if r.analysis and r.entry_price)
    entry = row.entry_price
    base = row.analysis.bars[0]
    flat = replace(base, low=entry, high=entry, close=entry)
    stop = replace(flat, low=entry * Decimal(".75"))
    target = replace(flat, high=entry * Decimal("1.2"))
    both = replace(stop, high=target.high)
    view = QWebEngineView()
    view.resize(900, 600)
    view.show()
    cases = (
        ((flat,), "Observed minutes", {}, ""),
        ((stop,), "Observed minutes", {"stop": 0}, ""),
        ((target,), "Observed minutes", {"target": 0}, ""),
        ((both,), "Observed minutes", {"stop": 0, "target": 0}, "order unknown"),
        ((both,), "Gaps in history", {"stop": 0, "target": 0}, "History incomplete"),
        (
            (
                flat,
                replace(stop, timestamp=base.timestamp + 60),
                replace(both, timestamp=base.timestamp + 120),
            ),
            "Observed minutes",
            {"stop": 1, "target": 2},
            "",
        ),
    )
    try:
        for bars, coverage, expected, qualifier in cases:
            sample = replace(
                row, coverage=coverage, analysis=replace(row.analysis, bars=bars)
            )
            loop = QEventLoop()
            view.loadFinished.connect(loop.quit)
            view.setHtml(
                "<style>.journal-price-chart{width:100%;height:300px}</style>"
                + to_xml(_price_chart(sample))
            )
            QTimer.singleShot(5000, loop.quit)
            loop.exec()
            view.loadFinished.disconnect(loop.quit)
            ready_loop = QEventLoop()
            ready = []
            wait_for_javascript(
                view,
                "[...document.querySelectorAll('[data-scenario-touch]')]"
                ".every(m => m.style.display === 'none' || m.hasAttribute('cx'))",
                lambda value, ready=ready, ready_loop=ready_loop: (
                    ready.append(value),
                    ready_loop.quit(),
                ),
                timeout=3,
            )
            QTimer.singleShot(4000, ready_loop.quit)
            ready_loop.exec()
            assert ready == [True]
            result_loop = QEventLoop()
            seen = []
            script = """(() => {
              const points=[...document.querySelectorAll('[data-price-point]')];
              const whiskers=[...document.querySelectorAll(
                '[data-price-event-whisker]')];
              const marks=[...document.querySelectorAll(
                '.journal-price-extreme,[data-scenario-touch]')]
                .filter(m=>m.style.display!=='none');
              return JSON.stringify({
                whiskerCount:whiskers.length,
                connected:marks.every(mark=>whiskers.some(w=>
                  Math.abs(Number(w.getAttribute('x1'))-Number(mark.getAttribute('cx')))<.01
                  && Number(mark.getAttribute('cy'))>=Number(w.getAttribute('y1'))-.01
                  && Number(mark.getAttribute('cy'))<=
                    Number(w.getAttribute('y2'))+.01)),
                touches:[...document.querySelectorAll('[data-scenario-touch]')]
                .filter(m => m.style.display !== 'none').map(m => ({
                  kind:m.dataset.scenarioTouch,
                  index:points.findIndex(p=>p.getAttribute('cx')===m.getAttribute('cx')),
                  onLevel:m.getAttribute('cy')===document.querySelector(
                    '.journal-scenario-hit[data-scenario-drag="'+m.dataset.scenarioTouch+'"]')
                    .getAttribute('y1'),
                  label:m.getAttribute('aria-label')
                }))});
            })()"""
            view.page().runJavaScript(
                script,
                lambda value, seen=seen, result_loop=result_loop: (
                    seen.append(json.loads(value)),
                    result_loop.quit(),
                ),
            )
            QTimer.singleShot(3000, result_loop.quit)
            result_loop.exec()
            assert seen
            assert seen[0]["connected"] and seen[0]["whiskerCount"] > 0
            assert {m["kind"]: m["index"] for m in seen[0]["touches"]} == expected
            assert all(
                m["onLevel"]
                and "not a confirmed fill" in m["label"]
                and qualifier in m["label"]
                for m in seen[0]["touches"]
            )
    finally:
        view.close()
        view.deleteLater()
        workbench.close()
