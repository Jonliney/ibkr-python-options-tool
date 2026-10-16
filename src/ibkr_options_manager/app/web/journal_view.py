"""Read-only dashboard and per-trade views in the workbench's visual system."""

# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from starhtml import (
    H1,
    H2,
    H3,
    Circle,
    Div,
    Form,
    Icon,
    Input,
    Line,
    Option,
    P,
    Polyline,
    Script,
    Select,
    Span,
    Svg,
    SvgPath,
    Table,
    Tbody,
    Td,
    Text,
    Th,
    Thead,
    Title,
    Tr,
)

from ...journal import JournalRow, entry_insights, summarize
from .components.ui.button import Button
from .components.ui.dropdown_menu import (
    DropdownMenu,
    DropdownMenuContent,
    DropdownMenuTrigger,
)
from .components.ui.input import Input as StarInput
from .components.ui.label import Label

D = Decimal


def percent(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:+.1f}%"


def money(value: Decimal | None, currency: str = "USD") -> str:
    if value is None:
        return "—"
    symbol = "$" if currency == "USD" else currency + " "
    return f"{'-' if value < 0 else ''}{symbol}{abs(value):,.2f}"


def stamp(value: int | None, *, full: bool = False) -> str:
    if value is None:
        return "Unknown"
    return datetime.fromtimestamp(value, UTC).strftime(
        "%d %b %Y · %H:%M UTC" if full else "%d %b"
    )


def trade_label(row: JournalRow) -> str:
    c = row.contract
    expiry = datetime.strptime(c.expiry, "%Y%m%d").strftime("%d %b %y")
    return f"{c.trading_class} {c.strike.normalize():f} {'Call' if c.right == 'C' else 'Put'} · {expiry}"


def _form(
    path: str, action: str, label: str, *, value: str = "", cls: str = "", **attrs: Any
) -> Any:
    return Form(
        Input(type="hidden", name="action", value=action),
        Input(type="hidden", name="trade_id", value=value),
        Button(label, type="submit", cls=cls, **attrs),
        action=path + "action",
        method="post",
        cls="journal-navigation",
    )


def app_navigation(path: str, active: str) -> Any:
    return Div(
        _form(
            path,
            "show-dashboard",
            "Dashboard",
            cls="journal-nav-tab" + (" is-active" if active == "dashboard" else ""),
            aria_current="page" if active == "dashboard" else None,
        ),
        _form(
            path,
            "show-positions",
            "Positions",
            cls="journal-nav-tab" + (" is-active" if active == "positions" else ""),
            aria_current="page" if active == "positions" else None,
        ),
        cls="journal-app-navigation",
        role="navigation",
        aria_label="Main navigation",
    )


def position_tabs(path: str, active: str, row: JournalRow | None = None) -> Any:
    return Div(
        _form(
            path,
            "show-management",
            "Management",
            cls="journal-position-tab"
            + (" is-active" if active == "management" else ""),
            disabled=row is not None and row.status.startswith("Closed"),
        ),
        _form(
            path,
            "show-analysis",
            "Analysis",
            cls="journal-position-tab" + (" is-active" if active == "analysis" else ""),
        ),
        cls="journal-position-tabs",
        role="navigation",
        aria_label="Position views",
    )


def _metric(label: str, value: str, detail: str, *, tone: str = "") -> Any:
    return Div(
        P(label, cls="journal-metric-label"),
        P(value, cls=f"journal-metric-value {tone}"),
        P(detail, cls="journal-caption"),
        cls="journal-metric",
    )


def _tone(value: Decimal | None) -> str:
    return (
        "journal-positive"
        if value is not None and value > 0
        else "journal-negative"
        if value is not None and value < 0
        else ""
    )


def _equity_chart(rows: tuple[JournalRow, ...]) -> Any:
    closed = sorted(
        (r for r in rows if r.realized_pnl is not None), key=lambda r: r.exit_at or 0
    )
    if not closed:
        return Div(
            P(
                "Your equity curve starts with the first closed trade.",
                cls="journal-muted",
            ),
            cls="journal-chart-empty",
        )
    values = [D(0)]
    for row in closed:
        values.append(values[-1] + (row.realized_pnl or D(0)))
    low, high = min(values), max(values)
    spread = max(high - low, D(1))

    def y(value: Decimal) -> float:
        return float(180 - (value - low) / spread * 145)

    points = " ".join(
        f"{30 + i / max(1, len(values) - 1) * 710:.1f},{y(v):.1f}"
        for i, v in enumerate(values)
    )
    return Div(
        Svg(
            Title(
                "Cumulative gross realized P&L from closed trades; excludes fees and FX"
            ),
            Text(f"{high:,.0f}", x=30, y=20, cls="journal-chart-label"),
            Text(f"{low:,.0f}", x=30, y=202, cls="journal-chart-label"),
            Line(x1=30, y1=y(D(0)), x2=740, y2=y(D(0)), cls="journal-zero-line"),
            Polyline(points=points, fill="none", cls="journal-equity-line"),
            viewBox="0 0 770 210",
            role="img",
            aria_label="Cumulative gross realized P&L",
            cls="journal-equity-chart",
        ),
        Div(
            Span(stamp(closed[0].exit_at)),
            Span(f"{len(closed)} closed trades"),
            Span(stamp(closed[-1].exit_at)),
            cls="journal-chart-axis",
        ),
    )


def insight_settings(stop: Decimal, target: Decimal) -> Any:
    def panel(**context: Any) -> Any:
        element = DropdownMenuContent(
            Div(
                Label("Stop level %", fr="journal-stop"),
                StarInput(
                    type="number",
                    name="journal_stop",
                    id="journal-stop",
                    value=str(stop),
                    min="0.1",
                    max="99.9",
                    step="0.1",
                    required=True,
                    autofocus=True,
                ),
                Label("Target %", fr="journal-target"),
                StarInput(
                    type="number",
                    name="journal_target",
                    id="journal-target",
                    value=str(target),
                    min="0.1",
                    max="10000",
                    step="0.1",
                    required=True,
                ),
                P(
                    "",
                    cls="journal-caption",
                    role="status",
                    data_entry_filter_status=True,
                ),
                cls="journal-insight-settings",
            ),
            align="end",
            cls="w-64 p-4",
        )(**context)
        # Reuse StarUI positioning/dismissal with dialog semantics for form fields.
        element.attrs["role"] = "dialog"
        return element

    def trigger(**context: Any) -> Any:
        element = DropdownMenuTrigger(
            Icon("lucide:funnel", cls="size-4"),
            size="icon",
            variant="ghost",
            aria_label="Entry timing settings",
        )(**context)
        element.attrs["aria-haspopup"] = "dialog"
        return element

    return DropdownMenu(trigger, panel)


def entry_timing_results(
    rows: tuple[JournalRow, ...], *, stop: Decimal, target: Decimal
) -> Any:
    insight = entry_insights(rows, stop=stop, target=target)
    target_text = f"+{target.normalize():f}%"
    definite_giveback = insight.gave_back + insight.retained
    return Div(
        Div(
            P(
                f"{insight.reached_target} / {insight.eligible}",
                cls="journal-insight-number",
            ),
            P(f"trades reached {target_text}", cls="journal-muted"),
            cls="journal-target-reach",
        ),
        Div(
            Div(
                P("Median dip before target", cls="journal-caption"),
                P(percent(insight.median_dip), cls="journal-insight-value"),
                P(f"{insight.dip_count} unambiguous paths", cls="journal-caption"),
            ),
            Div(
                P("Gave the gain back", cls="journal-caption"),
                P(
                    f"{insight.gave_back} / {definite_giveback}"
                    if definite_giveback
                    else "—",
                    cls="journal-insight-value",
                ),
                P("Returned to entry after target", cls="journal-caption"),
            ),
            cls="journal-insight-pair",
        ),
        H3("Recoveries after an early dip"),
        Table(
            Thead(
                Tr(
                    Th("Stop level", scope="col"),
                    Th("Hit before target", scope="col"),
                    Th("Later reached target", scope="col"),
                )
            ),
            Tbody(
                *(
                    Tr(
                        Td(f"-{r.stop_percent.normalize():f}%"),
                        Td(str(r.touched - r.ambiguous)),
                        Td(
                            f"{r.recovered} / {r.touched - r.ambiguous}"
                            if r.touched > r.ambiguous
                            else "—"
                        ),
                        cls="journal-selected-level" if r.stop_percent == stop else "",
                        aria_label=f"Selected stop -{stop.normalize():f}%"
                        if r.stop_percent == stop
                        else None,
                    )
                    for r in insight.recovery
                )
            ),
            cls="journal-recovery-table",
        ),
        P(
            f"{sum(r.ambiguous for r in insight.recovery)} ambiguous stop comparisons excluded. {insight.giveback_ambiguous + insight.followup_missing} giveback paths excluded. Observed bids through last capture; gaps excluded.",
            cls="journal-caption",
        ),
        cls="journal-entry-results",
    )


def _insight_script(path: str, generation: int) -> str:
    return """(() => {
      const root = document.querySelector('[data-entry-timing]');
      if (!root) return;
      const stop = root.querySelector('[name=journal_stop]');
      const target = root.querySelector('[name=journal_target]');
      const status = root.querySelector('[data-entry-filter-status]');
      const results = root.querySelector('[data-entry-results]');
      let timer, controller, revision = 0;
      const update = () => {
        const current = ++revision;
        clearTimeout(timer); controller?.abort();
        if (!stop.checkValidity() || !target.checkValidity()) {
          status.textContent = 'Enter a stop above 0 and below 100%, and a positive target.';
          results.removeAttribute('aria-busy'); return;
        }
        status.textContent = 'Updating…'; results.setAttribute('aria-busy', 'true');
        timer = setTimeout(async () => {
          controller = new AbortController();
          try {
            const response = await fetch(ENDPOINT, {
              method:'POST', signal:controller.signal,
              body:new URLSearchParams({journal_stop:stop.value, journal_target:target.value,
                generation:String(GENERATION), revision:String(current)})
            });
            if (!response.ok) throw Error('Update failed');
            const html = await response.text();
            if (current !== revision) return;
            results.innerHTML = html;
            status.textContent = 'Updated.';
          } catch (error) {
            if (current === revision && error.name !== 'AbortError')
              status.textContent = 'Could not update. Change a value to retry.';
          } finally {
            if (current === revision) results.removeAttribute('aria-busy');
          }
        }, 180);
      };
      stop.addEventListener('input', update); target.addEventListener('input', update);
      window.addEventListener('pagehide', () => {clearTimeout(timer); controller?.abort();}, {once:true});
    })();""".replace("ENDPOINT", repr(path + "journal-insights")).replace(
        "GENERATION", str(generation)
    )


def dashboard(
    rows: tuple[JournalRow, ...],
    path: str,
    *,
    stop: Decimal,
    target: Decimal,
    generation: int,
    error: str = "",
) -> Any:
    currencies = sorted({r.contract.currency for r in rows}) or ["USD"]
    groups = {
        currency: tuple(r for r in rows if r.contract.currency == currency)
        for currency in currencies
    }
    summaries = {currency: summarize(group) for currency, group in groups.items()}
    wins = sum(s.wins for s in summaries.values())
    losses = sum(s.losses for s in summaries.values())
    flat = sum(s.flat for s in summaries.values())
    priced = sum(s.priced_count for s in summaries.values())
    adverse = (
        sum(
            ((s.average_adverse or D(0)) * s.priced_count for s in summaries.values()),
            D(0),
        )
        / priced
        if priced
        else None
    )
    favorable = (
        sum(
            (
                (s.average_favorable or D(0)) * s.priced_count
                for s in summaries.values()
            ),
            D(0),
        )
        / priced
        if priced
        else None
    )
    realized = " / ".join(
        money(s.realized, currency) for currency, s in summaries.items()
    )
    return Div(
        Div(H1("Dashboard"), cls="journal-title-row"),
        P(error, role="alert", cls="journal-warning") if error else None,
        Div(
            _metric(
                "Win rate",
                f"{D(wins) / (wins + losses) * 100:.0f}%" if wins + losses else "—",
                f"{wins} wins · {losses} losses · {flat} flat",
            ),
            _metric(
                "Gross realized P&L",
                realized,
                "Closed trades · fees excluded"
                if len(currencies) == 1
                else "Separate totals by currency · no conversion",
                tone=_tone(next(iter(summaries.values())).realized)
                if len(currencies) == 1
                else "",
            ),
            _metric(
                "Average drawdown after entry",
                percent(adverse),
                f"{priced} trades with observed prices",
                tone="journal-negative",
            ),
            _metric(
                "Average best gain after entry",
                percent(favorable),
                "Observed path · includes after exit",
                tone="journal-positive",
            ),
            cls="journal-metrics",
        ),
        Div(
            Div(
                Div(H2("Realized performance"), cls="journal-section-heading"),
                *(
                    Div(Span(currency, cls="journal-caption"), _equity_chart(group))
                    for currency, group in groups.items()
                ),
                cls="journal-equity-section",
            ),
            Div(
                Div(
                    H2("Entry timing"),
                    insight_settings(stop, target),
                    cls="journal-section-heading",
                ),
                Div(
                    entry_timing_results(rows, stop=stop, target=target),
                    data_entry_results=True,
                    aria_live="polite",
                ),
                cls="journal-entry-insight",
                data_entry_timing=True,
            ),
            cls="journal-chart-layout",
        ),
        Div(
            H2("Trade history"),
            Span(f"{len(rows)} trades · all saved dates", cls="journal-caption"),
            cls="journal-section-heading",
        ),
        _history_table(rows, path),
        P(
            "Win rate excludes flat trades and unknown entries. Excursion averages exclude known gaps. Prices are one-minute bid observations; intraminute order and execution are not guaranteed.",
            cls="journal-footnote",
        ),
        Script(_insight_script(path, generation)),
        cls="journal-dashboard",
        data_journal_dashboard=True,
    )


def _history_table(rows: tuple[JournalRow, ...], path: str) -> Any:
    if not rows:
        return Div(
            H2("No saved trades yet"),
            P(
                "Collect price history and entry fills to begin. Unknown entry times remain visible without an invented result.",
                cls="journal-muted",
            ),
            cls="journal-empty",
        )
    return Div(
        Table(
            Thead(
                Tr(
                    *(
                        Th(label, scope="col")
                        for label in (
                            "Contract / entry",
                            "State",
                            "Actual P&L",
                            "Worst",
                            "Best",
                            "Price sequence",
                            "History",
                            "",
                        )
                    )
                )
            ),
            Tbody(
                *(
                    Tr(
                        Td(
                            Span(trade_label(row), cls="journal-contract"),
                            Span(
                                f"{stamp(row.entry_at)} · {row.quantity.normalize():f} contracts",
                                cls="journal-caption",
                            ),
                        ),
                        Td(row.status),
                        Td(
                            Span(
                                money(row.realized_pnl, row.contract.currency),
                                cls=_tone(row.realized_pnl),
                            ),
                            Span(percent(row.actual_return), cls="journal-caption"),
                        ),
                        Td(
                            percent(row.return_at(row.analysis.low))
                            if row.analysis
                            else "—",
                            cls=_tone(row.return_at(row.analysis.low))
                            if row.analysis
                            else "",
                        ),
                        Td(
                            percent(row.return_at(row.analysis.high))
                            if row.analysis
                            else "—",
                            cls=_tone(row.return_at(row.analysis.high))
                            if row.analysis
                            else "",
                        ),
                        Td(row.analysis.sequence if row.analysis else "Unknown"),
                        Td(row.coverage, cls="journal-caption"),
                        Td(
                            _form(
                                path,
                                "open-trade",
                                "View",
                                value=row.row_id,
                                cls="journal-table-link",
                                aria_label=f"Analyse {trade_label(row)} entered {stamp(row.entry_at)}",
                            )
                        ),
                    )
                    for row in rows
                )
            ),
            cls="journal-history-table",
        ),
        cls="journal-table-scroll",
    )


def _price_chart(row: JournalRow) -> Any:
    if row.analysis is None or not row.analysis.bars or row.entry_price is None:
        return Div(
            P("Price history is not available for this entry.", cls="journal-muted"),
            cls="journal-chart-empty",
        )
    bars = row.analysis.bars
    prices = [b.low for b in bars] + [b.high for b in bars] + [row.entry_price]
    low, high = min(prices), max(prices)
    spread = max(high - low, D(".01"))

    def x(timestamp: int) -> float:
        return (
            40
            + (timestamp - bars[0].timestamp)
            / max(60, bars[-1].timestamp - bars[0].timestamp)
            * 690
        )

    def y(value: Decimal) -> float:
        return float(230 - (value - low) / spread * 190)

    segments = [[bars[0]]]
    for bar in bars[1:]:
        if bar.timestamp - segments[-1][-1].timestamp != 60:
            segments.append([])
        segments[-1].append(bar)
    ranges = []
    closes = []
    for segment in segments:
        upper = [(x(b.timestamp), y(b.high)) for b in segment]
        lower = [(x(b.timestamp), y(b.low)) for b in reversed(segment)]
        area = (
            "M " + " L ".join(f"{xx:.1f} {yy:.1f}" for xx, yy in upper + lower) + " Z"
        )
        ranges.append(SvgPath(d=area, cls="journal-price-range"))
        if len(segment) == 1:
            closes.append(
                Circle(
                    cx=x(segment[0].timestamp),
                    cy=y(segment[0].close),
                    r=2,
                    cls="journal-price-dot",
                )
            )
        else:
            points = " ".join(f"{x(b.timestamp):.1f},{y(b.close):.1f}" for b in segment)
            closes.append(
                Polyline(points=points, fill="none", cls="journal-price-line")
            )
    return Div(
        Svg(
            Title("Observed minute bid range and closes, including prices after exit"),
            Text(f"{high:.2f}", x=1, y=42, cls="journal-chart-label"),
            Text(f"{low:.2f}", x=1, y=234, cls="journal-chart-label"),
            *ranges,
            Line(
                x1=40,
                x2=730,
                y1=y(row.entry_price),
                y2=y(row.entry_price),
                cls="journal-zero-line",
            ),
            *closes,
            *(
                Line(
                    x1=x(row.exit_at),
                    x2=x(row.exit_at),
                    y1=25,
                    y2=245,
                    cls="journal-exit-line",
                )
                for b in bars
                if row.exit_at is not None
                and b.timestamp <= row.exit_at < b.timestamp + 60
            ),
            viewBox="0 0 770 265",
            role="img",
            aria_label="Bid price path after entry",
            cls="journal-price-chart",
        ),
        Div(
            Span(stamp(bars[0].timestamp, full=True)),
            Span("Range / close · dashed = entry · dotted = exit"),
            Span(stamp(bars[-1].timestamp, full=True)),
            cls="journal-chart-axis",
        ),
        P(
            "Elapsed time (UTC). Gaps between observations are left unconnected.",
            cls="journal-caption",
        ),
    )


def position_analysis(
    row: JournalRow | None,
    rows: tuple[JournalRow, ...],
    path: str,
    *,
    stop: Decimal,
    target: Decimal,
) -> Any:
    if row is None:
        return Div(
            H1("Position analysis"),
            position_tabs(path, "analysis"),
            P(
                "No saved entry is available for this position yet.",
                cls="journal-muted",
            ),
            cls="journal-analysis journal-empty",
        )
    analysis = row.analysis
    related = tuple(r for r in rows if r.contract.con_id == row.contract.con_id)
    c = row.contract.currency

    def outcome(label: str, price: Decimal | None, detail: str) -> Any:
        value = row.return_at(price)
        return _metric(
            label,
            percent(value),
            f"{money(row.pnl_at(price), c)} · {detail}",
            tone=_tone(value),
        )

    return Div(
        Div(
            Div(
                H1(trade_label(row)),
                P(
                    f"{row.status} · {row.quantity.normalize():f} contracts · {row.coverage}",
                    cls="journal-muted",
                ),
            ),
            _form(path, "show-dashboard", "Back to dashboard", cls="journal-back"),
            cls="journal-title-row",
        ),
        position_tabs(path, "analysis", row),
        Form(
            Label("Entry / trade", fr="journal-entry"),
            Select(
                *(
                    Option(
                        f"{stamp(r.entry_at, full=True)} · {r.status}",
                        value=r.row_id,
                        selected=r.row_id == row.row_id,
                    )
                    for r in related
                ),
                name="trade_id",
                id="journal-entry",
            ),
            Input(type="hidden", name="action", value="open-trade"),
            Button("View trade", type="submit"),
            action=path + "action",
            method="post",
            cls="journal-entry-picker",
        )
        if len(related) > 1
        else None,
        Div(
            _metric(
                "Entry price", money(row.entry_price, c), stamp(row.entry_at, full=True)
            ),
            _metric(
                "Actual gross P&L",
                money(row.realized_pnl, c),
                percent(row.actual_return)
                if row.realized_pnl is not None
                else "Available after a fully matched close",
                tone=_tone(row.realized_pnl),
            ),
            _metric(
                "Average observed price",
                money(analysis.average if analysis else None, c),
                "Arithmetic mean of minute closes",
            ),
            cls="journal-metrics journal-metrics-three",
        ),
        Div(
            outcome(
                "Worst observed",
                analysis.low if analysis else None,
                money(analysis.low if analysis else None, c) + " bid",
            ),
            outcome(
                "Best observed",
                analysis.high if analysis else None,
                money(analysis.high if analysis else None, c) + " bid",
            ),
            outcome(
                "Hold to final session",
                row.final_price,
                "Final bid estimate · not settlement",
            )
            if row.final_kind == "expiry_bid"
            else _metric("Hold to expiration", "—", "Final-session value not captured"),
            cls="journal-metrics journal-metrics-three",
        ),
        Div(
            H2("Price path after entry"),
            Span(
                f"{len(analysis.bars) if analysis else 0} observed minutes · {c}",
                cls="journal-caption",
            ),
            cls="journal-section-heading",
        ),
        _price_chart(row),
        Div(
            Div(
                H2("Order of extremes"),
                P(
                    analysis.sequence if analysis else "Unknown",
                    cls="journal-insight-label",
                ),
                P(
                    f"Low: {stamp(analysis.low_at, full=True)} · High: {stamp(analysis.high_at, full=True)}"
                    if analysis
                    else "Entry timing is needed to calculate the path.",
                    cls="journal-caption",
                ),
            ),
            Div(
                H2(f"-{stop.normalize():f}% stop / +{target.normalize():f}% target"),
                P(
                    analysis.threshold_sequence if analysis else "Unknown",
                    cls="journal-insight-label",
                ),
                P(
                    "Hypothetical bid levels, including prices after sale. Same-minute ordering remains unknown.",
                    cls="journal-caption",
                ),
            ),
            cls="journal-sequence-layout",
        ),
        P(row.warning, cls="journal-warning") if row.warning else None,
        Div(H2("Trade fills"), _fill_table(row), cls="journal-fill-section"),
        Div(
            H2("Contract position observations"),
            _observation_table(row),
            cls="journal-fill-section",
        ),
        P(
            "Gross estimates use original entry quantity and contract multiplier. Entry minute excluded; scale-ins start after the final buy. Actual results come only from matched fills. Fees, FX, exercise and assignment are excluded.",
            cls="journal-footnote",
        ),
        cls="journal-analysis",
        data_position_analysis=True,
    )


def _fill_table(row: JournalRow) -> Any:
    if row.trade is None:
        return P(
            "The holding was observed, but its entry fills could not be reconstructed.",
            cls="journal-muted",
        )
    return Table(
        Thead(
            Tr(
                *(
                    Th(label, scope="col")
                    for label in (
                        "Time (UTC)",
                        "Event",
                        "Quantity",
                        "Price",
                        "Execution",
                    )
                )
            )
        ),
        Tbody(
            *(
                Tr(
                    Td(stamp(f.timestamp, full=True)),
                    Td("Entry" if f.side == "BOT" else "Exit"),
                    Td(f"{f.quantity.normalize():f}"),
                    Td(money(f.price, row.contract.currency)),
                    Td(f.exec_id),
                )
                for f in row.trade.fills
            )
        ),
        cls="journal-fill-table",
    )


def _observation_table(row: JournalRow) -> Any:
    if not row.observations:
        return P("No position snapshots saved for this contract.", cls="journal-muted")
    return Div(
        Table(
            Thead(
                Tr(
                    *(
                        Th(label, scope="col")
                        for label in (
                            "Observed (UTC)",
                            "Held quantity",
                            "Average premium",
                            "State",
                        )
                    )
                )
            ),
            Tbody(
                *(
                    Tr(
                        Td(stamp(o.timestamp, full=True)),
                        Td(f"{o.quantity.normalize():f}"),
                        Td(
                            money(
                                o.average_cost / row.contract.multiplier,
                                row.contract.currency,
                            )
                        ),
                        Td("Flat observed" if o.quantity == 0 else "Holding observed"),
                    )
                    for o in row.observations[-20:]
                )
            ),
            cls="journal-fill-table",
        ),
        P(
            "Last 20 quantity or cost changes. A flat observation alone does not establish a sell fill or realized P&L.",
            cls="journal-caption",
        ),
        cls="journal-table-scroll",
    )
