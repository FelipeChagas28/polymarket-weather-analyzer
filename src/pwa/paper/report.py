"""Terminal output for paper-trading: daily summary and full report."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pwa.paper import db as pdb
from pwa.paper.engine import DbRunReport, PlacedBet, ResolvedBet, RunStage, Summary, compute_summary


def _fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def render_daily_summary(
    conn: sqlite3.Connection,
    resolved: list[ResolvedBet],
    placed: list[PlacedBet],
    console: Console,
) -> None:
    s = compute_summary(conn)
    today = datetime.now(timezone.utc).date().isoformat()

    resolved_won = sum(1 for r in resolved if r.status == "won")
    resolved_lost = sum(1 for r in resolved if r.status == "lost")
    resolved_pnl = sum(r.profit_loss for r in resolved)

    header_lines = [
        f"[bold]Banca atual:[/bold] [yellow]{_fmt_money(s.bankroll_current)}[/yellow] "
        f"(inicial {_fmt_money(s.bankroll_start)}, [magenta]{s.roi_pct:+.1f}%[/magenta])",
        f"[bold]Apostas abertas:[/bold] {s.n_open}  |  "
        f"[bold]Resolvidas hoje:[/bold] {len(resolved)} "
        f"([green]{resolved_won}W[/green]/[red]{resolved_lost}L[/red], "
        f"[{'green' if resolved_pnl >= 0 else 'red'}]{resolved_pnl:+.2f}[/{'green' if resolved_pnl >= 0 else 'red'}])",
    ]
    console.print(Panel("\n".join(header_lines), title=f"Paper-trading day {today}", border_style="cyan"))

    if resolved:
        table = Table(title="Resolvidas hoje", show_lines=False)
        table.add_column("Evento", style="white", overflow="fold")
        table.add_column("Bin apostado", style="cyan")
        table.add_column("Side", justify="center")
        table.add_column("Bin que ganhou", style="yellow")
        table.add_column("Preço", justify="right", style="dim")
        table.add_column("Stake", justify="right")
        table.add_column("Resultado", justify="center")
        table.add_column("P/L", justify="right")
        for r in resolved:
            side_style = "green" if r.side == "YES" else "blue"
            if r.status == "won":
                status_cell = "[bold green]WON[/bold green]"
                pnl_cell = f"[green]+{_fmt_money(r.profit_loss)}[/green]"
            elif r.status == "lost":
                status_cell = "[red]LOST[/red]"
                pnl_cell = f"[red]{_fmt_money(r.profit_loss)}[/red]"
            else:
                status_cell = "[yellow]VOID[/yellow]"
                pnl_cell = "[dim]-[/dim]"
            table.add_row(
                r.event_slug[:38],
                r.bin_label,
                f"[{side_style}]{r.side}[/{side_style}]",
                r.realized_bin or "[dim]?[/dim]",
                f"{r.price_entry:.3f}",
                _fmt_money(r.stake),
                status_cell,
                pnl_cell,
            )
        console.print(table)

    if placed:
        table = Table(title="Novas apostas hoje", show_lines=False)
        table.add_column("Evento", style="white", overflow="fold")
        table.add_column("Bin", style="cyan")
        table.add_column("Side", justify="center")
        table.add_column("Preço", justify="right")
        table.add_column("Stake", justify="right", style="yellow")
        for p in placed:
            side_style = "green" if p.side == "YES" else "blue"
            table.add_row(
                p.event_slug[:38],
                p.bin_label,
                f"[{side_style}]{p.side}[/{side_style}]",
                f"{p.price_entry:.3f}",
                _fmt_money(p.stake),
            )
        console.print(table)

    _render_metrics(s, console)
    _render_suggestions(s, console)


def _render_metrics(s: Summary, console: Console) -> None:
    started_at = "-"  # not stored in Summary; pulled below if useful
    lines = [
        f"[bold]Apostas totais:[/bold] {s.n_won + s.n_lost + s.n_void + s.n_open}  |  "
        f"[bold]Resolvidas:[/bold] {s.n_resolved} ({s.n_won}W/{s.n_lost}L/{s.n_void}V)  |  "
        f"[bold]Winrate:[/bold] [magenta]{s.winrate*100:.1f}%[/magenta]  |  "
        f"[bold]ROI:[/bold] [magenta]{s.roi_pct:+.1f}%[/magenta]",
    ]
    if s.by_agreement:
        parts = []
        for agr in ("strong", "moderate", "weak"):
            if agr not in s.by_agreement:
                continue
            w, l = s.by_agreement[agr]
            wr = w / (w + l) * 100 if (w + l) > 0 else 0.0
            parts.append(f"{agr} {w}W/{l}L ({wr:.0f}%)")
        if parts:
            lines.append("[bold]Por agreement:[/bold] " + "  ".join(parts))
    console.print(Panel("\n".join(lines), title="Métricas gerais", border_style="magenta"))


def _render_suggestions(s: Summary, console: Console) -> None:
    suggestions: list[str] = []
    if s.n_resolved >= 10:
        moderate = s.by_agreement.get("moderate", (0, 0))
        m_total = moderate[0] + moderate[1]
        if m_total >= 5 and moderate[0] / m_total < 0.4:
            suggestions.append(
                "Apostas com agreement=moderate estão com winrate <40%. "
                "Considere testar [bold]--mode strict[/bold] (só agreement=strong) numa rodada paralela."
            )
        if s.roi_pct < -20:
            suggestions.append(
                f"ROI em {s.roi_pct:+.1f}%. Pode valer rodar `pwa calibrate <city>` "
                "para verificar se o bias correction está adequado."
            )
        if s.n_won + s.n_lost >= 30 and s.winrate < 0.45:
            suggestions.append(
                f"Winrate em {s.winrate*100:.1f}% após {s.n_won + s.n_lost} apostas resolvidas. "
                "Investigar se o consensus está calibrado ou se vale apertar o threshold de edge."
            )
    if suggestions:
        body = "\n".join(f"- {s}" for s in suggestions)
        console.print(Panel(body, title="Sugestões", border_style="yellow"))


def render_full_report(conn: sqlite3.Connection, console: Console, limit: int = 50) -> None:
    s = compute_summary(conn)
    start_at = pdb.get_state(conn, "started_at") or "-"
    mode = pdb.get_state(conn, "mode") or "auto"

    header = (
        f"[bold]Modo:[/bold] {mode}   "
        f"[bold]Iniciado:[/bold] {start_at}\n"
        f"[bold]Banca:[/bold] {_fmt_money(s.bankroll_start)} -> "
        f"[yellow]{_fmt_money(s.bankroll_current)}[/yellow]  "
        f"([magenta]{s.roi_pct:+.1f}%[/magenta])\n"
        f"[bold]Apostas:[/bold] {s.n_open} abertas | {s.n_won}W / {s.n_lost}L / {s.n_void}V resolvidas"
    )
    console.print(Panel(header, title="Resumo geral do paper-trading", border_style="cyan"))

    # By city
    by_city = conn.execute(
        "SELECT city_key, "
        "SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS w, "
        "SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) AS l, "
        "ROUND(COALESCE(SUM(profit_loss), 0), 2) AS pnl "
        "FROM bets WHERE status IN ('won','lost') "
        "GROUP BY city_key ORDER BY pnl DESC"
    ).fetchall()
    if by_city:
        t = Table(title="Performance por cidade", show_lines=False)
        t.add_column("Cidade", style="cyan")
        t.add_column("W", justify="right", style="green")
        t.add_column("L", justify="right", style="red")
        t.add_column("Winrate", justify="right")
        t.add_column("P/L", justify="right")
        for r in by_city:
            wr = r["w"] / (r["w"] + r["l"]) * 100 if (r["w"] + r["l"]) > 0 else 0
            pnl = r["pnl"] or 0
            pnl_style = "green" if pnl >= 0 else "red"
            t.add_row(r["city_key"], str(r["w"]), str(r["l"]), f"{wr:.0f}%",
                      f"[{pnl_style}]{_fmt_money(pnl)}[/{pnl_style}]")
        console.print(t)

    # Recent bets
    bets = pdb.all_bets(conn, limit=limit)
    if bets:
        t = Table(title=f"Últimas {len(bets)} apostas", show_lines=False)
        t.add_column("Quando", style="dim")
        t.add_column("Cidade", style="cyan")
        t.add_column("Bin", style="white")
        t.add_column("Side", justify="center")
        t.add_column("Preço", justify="right")
        t.add_column("Stake", justify="right")
        t.add_column("Agree", justify="center")
        t.add_column("Status", justify="center")
        t.add_column("Realized", style="yellow")
        t.add_column("P/L", justify="right")
        for b in bets:
            side_style = "green" if b.side == "YES" else "blue"
            status_color = {"won": "green", "lost": "red", "void": "yellow", "open": "dim"}.get(b.status, "white")
            pnl_str = (
                f"[{'green' if (b.profit_loss or 0) >= 0 else 'red'}]"
                f"{_fmt_money(b.profit_loss)}[/]"
            ) if b.profit_loss is not None else "[dim]-[/dim]"
            t.add_row(
                b.placed_at[:16],
                b.city_key,
                b.bin_label[:14],
                f"[{side_style}]{b.side}[/{side_style}]",
                f"{b.price_entry:.3f}",
                _fmt_money(b.stake),
                b.agreement,
                f"[{status_color}]{b.status}[/{status_color}]",
                b.realized_bin or "[dim]-[/dim]",
                pnl_str,
            )
        console.print(t)


_STATUS_GLYPH = {
    "ok": "[green]OK  [/green]",
    "partial": "[yellow]WARN[/yellow]",
    "fail": "[red]FAIL[/red]",
    "skip": "[dim]SKIP[/dim]",
}


def _short_db_name(path: str) -> str:
    """Last path segment without extension — `~/.pwa/paper_strict.db` -> `paper_strict`."""
    tail = path.replace("\\", "/").rsplit("/", 1)[-1]
    if tail.endswith(".db"):
        tail = tail[:-3]
    return tail


def render_run_report(
    console: Console,
    stages: list[RunStage],
    db_reports: list[DbRunReport],
) -> None:
    """Final end-of-run summary: stage checklist + per-DB consolidated table."""
    if not stages and not db_reports:
        return

    checklist_lines: list[str] = []
    for st in stages:
        glyph = _STATUS_GLYPH.get(st.status, st.status)
        line = f"{glyph} {st.name}"
        if st.detail:
            line += f"  [dim]({st.detail})[/dim]"
        checklist_lines.append(line)

    for rep in db_reports:
        status = "ok" if rep.ok else ("skip" if "não inicializado" in rep.note or "congelado" in rep.note else "fail")
        glyph = _STATUS_GLYPH[status]
        head = f"{glyph} {_short_db_name(rep.db)}  [dim](mode={rep.mode})[/dim]"
        if not rep.ok:
            checklist_lines.append(f"{head}  [dim]{rep.note}[/dim]")
        else:
            checklist_lines.append(
                f"{head}  [dim]+{rep.n_placed_today} criadas, {rep.n_resolved_today} encerradas[/dim]"
            )

    if checklist_lines:
        console.print(Panel("\n".join(checklist_lines), title="Checklist da rodada", border_style="cyan"))

    ran = [r for r in db_reports if r.ok]
    if not ran:
        return

    # Modo é redundante (já aparece no checklist). Banca/ROI ficam em uma só coluna.
    t = Table(title="Resumo dos testes", show_lines=False, pad_edge=False, padding=(0, 1))
    t.add_column("Teste", style="cyan", no_wrap=True)
    t.add_column("Criadas hoje", justify="right", style="green", no_wrap=True)
    t.add_column("Encerradas (W/L/V)", justify="center", no_wrap=True)
    t.add_column("Em aberto", justify="right", no_wrap=True)
    t.add_column("Banca (ROI)", justify="right", style="yellow", no_wrap=True)

    tot_placed = 0
    tot_resolved = 0
    tot_won = 0
    tot_lost = 0
    tot_void = 0
    tot_pnl = 0.0
    tot_open = 0

    for rep in ran:
        enc = f"{rep.n_resolved_today} ({rep.n_won_today}/{rep.n_lost_today}/{rep.n_void_today})"
        roi_color = "green" if rep.roi_pct >= 0 else "red"
        banca = f"{_fmt_money(rep.bankroll_after)} [{roi_color}]({rep.roi_pct:+.1f}%)[/{roi_color}]"
        t.add_row(
            f"{_short_db_name(rep.db)} [dim]({rep.mode})[/dim]",
            str(rep.n_placed_today),
            enc,
            str(rep.n_open_now),
            banca,
        )
        tot_placed += rep.n_placed_today
        tot_resolved += rep.n_resolved_today
        tot_won += rep.n_won_today
        tot_lost += rep.n_lost_today
        tot_void += rep.n_void_today
        tot_pnl += rep.pnl_today
        tot_open += rep.n_open_now

    if len(ran) > 1:
        t.add_row(
            "[bold]TOTAL[/bold]",
            f"[bold]{tot_placed}[/bold]",
            f"[bold]{tot_resolved} ({tot_won}/{tot_lost}/{tot_void})[/bold]",
            f"[bold]{tot_open}[/bold]",
            "",
        )

    console.print(t)


def render_status(conn: sqlite3.Connection, console: Console) -> None:
    s = compute_summary(conn)
    open_count = s.n_open
    next_resolution_row = conn.execute(
        "SELECT MIN(target_date) AS d FROM bets WHERE status = 'open'"
    ).fetchone()
    next_date = next_resolution_row["d"] if next_resolution_row else None
    body = (
        f"[bold]Banca:[/bold] [yellow]{_fmt_money(s.bankroll_current)}[/yellow] "
        f"([magenta]{s.roi_pct:+.1f}%[/magenta] vs {_fmt_money(s.bankroll_start)})\n"
        f"[bold]Apostas abertas:[/bold] {open_count}\n"
        f"[bold]Próxima resolução:[/bold] {next_date or '[dim]-[/dim]'}"
    )
    console.print(Panel(body, title="Paper-trading status", border_style="cyan"))
