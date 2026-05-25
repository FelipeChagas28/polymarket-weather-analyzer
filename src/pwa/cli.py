from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pwa.analysis.consensus import ConsensusRow, compute_consensus
from pwa.analysis.edge import EdgeRow, evaluate_bin
from pwa.analysis.report import AnalysisContext, render
from pwa.backtest.calibrate import calibrate_city, summarize
from pwa.cache import get_cached_bias, put_cached_bias
from pwa.models.bias import BiasReport, apply_bias, compute_bias
from pwa.models.kde import bins_to_probs
from pwa.paper import db as pdb
from pwa.paper.engine import (
    DbRunReport,
    RunStage,
    compute_summary,
    place_bets_for_event,
    resolve_open_bets,
)
from pwa.paper.report import render_daily_summary, render_full_report, render_run_report, render_status
from pwa.polymarket.clob import best_yes_ask_from_market, best_yes_bid_from_market
from pwa.polymarket.gamma import GammaClient, event_markets
from pwa.polymarket.parser import detect_unit, parse_event_bins, parse_event_title
from pwa.weather.open_meteo import EnsembleResult
from pwa.weather.sources import (
    SourceForecast,
    fetch_open_meteo_ensemble,
    fetch_open_meteo_per_model,
    fetch_yr_no,
)
from pwa.weather.stations import get_station

app = typer.Typer(add_completion=False, help="Polymarket Weather Analyzer")
paper_app = typer.Typer(add_completion=False, help="Paper-trading (dinheiro fictício, persistido em ~/.pwa/paper.db)")
app.add_typer(paper_app, name="paper")
console = Console()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    ctx: AnalysisContext
    edge_rows: list[EdgeRow]
    consensus_rows: list[ConsensusRow]
    event_slug: str
    event_title: str
    target_date: date
    city_key: str


def run_analysis(
    event_id_or_slug: str,
    *,
    no_bias: bool = False,
    lookback: int = 60,
    out: Console | None = None,
) -> AnalysisResult | None:
    """Full pipeline: fetch event, all sources in parallel, bias, consensus, edge rows.

    Returns None (and prints diagnostics to `out`) if the event can't be analyzed.
    Reuses the same logic as the `analyze` CLI command so paper-trading can drive it.
    """
    out = out or console
    with GammaClient() as gamma:
        ev = gamma.get_event(event_id_or_slug)

    title = ev.get("title", "")
    info = parse_event_title(title, end_date_iso=ev.get("endDate"))
    if info is None:
        out.print(f"[red]Não consegui parsear o título: {title!r}[/red]")
        return None

    station = get_station(info.city_key)
    if station is None:
        out.print(f"[red]Cidade desconhecida: {info.city_raw!r} (key={info.city_key}).[/red]")
        return None

    markets = list(event_markets(ev))
    bin_pairs = parse_event_bins(markets)
    if not bin_pairs:
        out.print(f"[red]Nenhum bin reconhecido em {ev.get('slug')}[/red]")
        return None

    bins = [b for _, b in bin_pairs]
    unit = detect_unit(bins)
    unit_label = "°C" if unit == "C" else "°F"

    out.print(
        f"[dim]Buscando previsões em paralelo (Open-Meteo ensemble + per-model + yr.no) "
        f"para {station.display_name} em {info.target_date} (unit={unit_label})...[/dim]"
    )

    def _fetch_ens() -> SourceForecast:
        return fetch_open_meteo_ensemble(station.lat, station.lon, info.target_date, station.tz, info.direction, unit=unit)

    def _fetch_per_model() -> list[SourceForecast]:
        try:
            return fetch_open_meteo_per_model(station.lat, station.lon, info.target_date, station.tz, info.direction, unit=unit)
        except Exception as e:
            out.print(f"[yellow]Open-Meteo per-model falhou ({type(e).__name__}: {e}); seguindo.[/yellow]")
            return []

    def _fetch_yr() -> SourceForecast | None:
        try:
            return fetch_yr_no(station.lat, station.lon, info.target_date, station.tz, info.direction, unit=unit)
        except Exception as e:
            out.print(f"[yellow]yr.no falhou ({type(e).__name__}: {e}); seguindo sem essa fonte.[/yellow]")
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_ens = pool.submit(_fetch_ens)
        fut_per = pool.submit(_fetch_per_model)
        fut_yr = pool.submit(_fetch_yr)
        ens_source = fut_ens.result()
        per_model_sources = fut_per.result()
        yr_source = fut_yr.result()

    ens = EnsembleResult(
        target_date=ens_source.target_date,
        direction=info.direction,
        members_daily=ens_source.samples,
        n_members=ens_source.n_members,
    )

    bias_report: BiasReport | None = None
    samples = ens.members_daily
    if not no_bias:
        bias_report = get_cached_bias(station.city_key, info.direction, unit)
        if bias_report is not None:
            out.print(
                f"[dim]Bias correction em cache "
                f"({bias_report.mean_bias:+.2f}{unit_label}, n={bias_report.n_days}d).[/dim]"
            )
        else:
            out.print(f"[dim]Calculando bias correction ({lookback}d de histórico)...[/dim]")
            try:
                bias_report = compute_bias(
                    station.lat, station.lon, station.tz, info.direction,
                    today=date.today(), lookback_days=lookback, unit=unit,
                )
                put_cached_bias(station.city_key, info.direction, unit, bias_report)
            except Exception as e:
                out.print(f"[yellow]Bias correction falhou ({type(e).__name__}: {e}); seguindo sem correção.[/yellow]")
                bias_report = None
        if bias_report is not None:
            samples = apply_bias(samples, bias_report)

    om_probs = bins_to_probs(samples, bins)

    yes_asks: list[float | None] = []
    yes_bids: list[float | None] = []
    om_only_sides: list[str] = []
    for (market, b), bp in zip(bin_pairs, om_probs):
        ask = best_yes_ask_from_market(market)
        bid = best_yes_bid_from_market(market)
        om_row = evaluate_bin(b, bp.p_model, ask, bid)
        yes_asks.append(ask)
        yes_bids.append(bid)
        om_only_sides.append(om_row.side if om_row.side_price is not None else "—")

    consensus_sources: list[SourceForecast] = [
        SourceForecast(
            source_name="open-meteo-ensemble",
            target_date=ens.target_date,
            samples=samples,
            is_ensemble=True,
            n_members=ens.n_members,
            unit=unit,
        )
    ]
    consensus_sources.extend(per_model_sources)
    if yr_source is not None:
        consensus_sources.append(yr_source)

    consensus_rows = compute_consensus(consensus_sources, bins, yes_asks, yes_bids, om_only_sides)

    rows: list[EdgeRow] = []
    for (market, b), crow in zip(bin_pairs, consensus_rows):
        ask = best_yes_ask_from_market(market)
        bid = best_yes_bid_from_market(market)
        rows.append(evaluate_bin(b, crow.consensus_prob, ask, bid, agreement=crow.agreement))

    ctx = AnalysisContext(
        event_title=title,
        event_slug=ev.get("slug", ""),
        station=station,
        ensemble=ens,
        bias=bias_report,
        unit=unit,
    )
    return AnalysisResult(
        ctx=ctx, edge_rows=rows, consensus_rows=consensus_rows,
        event_slug=ev.get("slug", ""),
        event_title=title,
        target_date=info.target_date,
        city_key=info.city_key,
    )


@app.command("list")
def list_cmd(
    days: int = typer.Option(7, "--days", "-d", help="Janela em dias à frente para incluir eventos"),
    show_closed: bool = typer.Option(False, "--all", help="Inclui eventos já fechados (marcados como closed pela Polymarket)"),
) -> None:
    """Lista eventos ativos de temperatura diária na Polymarket."""
    now = datetime.now(timezone.utc)
    with GammaClient() as gamma:
        events = gamma.list_temperature_events()

    rows = []
    for ev in events:
        if not show_closed and ev.get("closed"):
            continue
        end = _parse_iso(ev.get("endDate"))
        if end is None:
            continue
        delta_days = (end - now).total_seconds() / 86400
        if delta_days < -1 or delta_days > days:
            continue
        rows.append((ev, end, delta_days))

    rows.sort(key=lambda r: r[1])

    table = Table(title=f"Mercados de temperatura ativos (próximos {days}d)", show_lines=False)
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("Slug", style="white", overflow="fold")
    table.add_column("Cidade / Pergunta", style="white")
    table.add_column("Resolução (UTC)", style="yellow")
    table.add_column("h restantes", justify="right", style="magenta")
    table.add_column("# bins", justify="right")
    table.add_column("Volume", justify="right", style="green")

    for ev, end, dd in rows:
        markets = list(event_markets(ev))
        vol = float(ev.get("volume") or 0)
        hours = dd * 24
        table.add_row(
            str(ev.get("id")),
            ev.get("slug", ""),
            ev.get("title", "")[:60],
            end.strftime("%Y-%m-%d %H:%M"),
            f"{hours:+.1f}",
            str(len(markets)),
            f"${vol:,.0f}",
        )

    if not rows:
        console.print("[yellow]Nenhum evento de temperatura encontrado na janela.[/yellow]")
        console.print("[dim]Tente aumentar --days ou usar --all para incluir closed.[/dim]")
        return

    console.print(table)
    console.print(f"[dim]Total: {len(rows)} eventos[/dim]")


@app.command("analyze")
def analyze_cmd(
    event: str = typer.Argument(..., help="ID numérico ou slug do evento"),
    no_bias: bool = typer.Option(False, "--no-bias", help="Pula correção de viés histórica"),
    lookback: int = typer.Option(60, "--lookback", help="Dias de histórico para bias correction"),
) -> None:
    """Análise estatística completa de um evento de temperatura."""
    result = run_analysis(event, no_bias=no_bias, lookback=lookback)
    if result is None:
        raise typer.Exit(code=2)
    render(result.ctx, result.edge_rows, consensus_rows=result.consensus_rows, console=console)


@app.command("calibrate")
def calibrate_cmd(
    city: str = typer.Argument(..., help="city_key (ex: nyc, london, miami)"),
    n: int = typer.Option(30, "--n", help="Número de eventos passados a usar"),
    lookback: int = typer.Option(60, "--lookback", help="Dias de histórico para bias"),
) -> None:
    """Backtest de calibração em mercados de temperatura já resolvidos."""
    console.print(f"[dim]Calibrando {city} em até {n} eventos resolvidos...[/dim]")
    points = calibrate_city(city, n=n, lookback_days=lookback)
    summary = summarize(points)

    table = Table(title=f"Calibração — {city}")
    table.add_column("Métrica", style="cyan")
    table.add_column("Valor", justify="right")
    table.add_row("n eventos avaliados", f"{int(summary['n'])}")
    table.add_row("Brier médio (multi-bin)", f"{summary['mean_brier']:.4f}")
    table.add_row("Log-loss médio", f"{summary['mean_log_loss']:.4f}")
    table.add_row("P(realizado) médio", f"{summary['mean_p_realized']*100:.1f}%")
    console.print(table)

    if points:
        console.print("[dim]Eventos individuais:[/dim]")
        detail = Table(show_lines=False)
        detail.add_column("date", style="yellow")
        detail.add_column("realized bin", style="white")
        detail.add_column("p_model", justify="right", style="cyan")
        detail.add_column("log-loss", justify="right", style="magenta")
        for p in sorted(points, key=lambda x: x.target_date, reverse=True)[:20]:
            detail.add_row(p.target_date, p.realized_bin, f"{p.p_realized*100:.1f}%", f"{p.log_loss:.3f}")
        console.print(detail)


# ---------------------------------------------------------------------------
# Paper trading subcommands
# ---------------------------------------------------------------------------


def _db_option() -> typer.Option:
    return typer.Option(str(pdb.DEFAULT_DB_PATH), "--db", help="Caminho do SQLite (default: ~/.pwa/paper.db)")


@paper_app.command("init")
def paper_init(
    bankroll: float = typer.Option(10.0, "--bankroll", help="Banca inicial em USD"),
    mode: str = typer.Option(
        "auto", "--mode",
        help="auto (toda BUY) | strict (só agreement=strong) | strongbuy (só recomendação STRONG BUY)",
    ),
    db: str = _db_option(),
    force: bool = typer.Option(False, "--force", help="Sobrescreve state existente"),
) -> None:
    """Inicializa o banco de paper-trading."""
    if mode not in ("auto", "strict", "strongbuy"):
        console.print(f"[red]mode deve ser 'auto', 'strict' ou 'strongbuy' (recebido: {mode!r})[/red]")
        raise typer.Exit(code=2)
    with pdb.session(db) as conn:
        if pdb.is_initialized(conn) and not force:
            console.print(f"[yellow]DB em {db} já está inicializado. Use --force para sobrescrever.[/yellow]")
            raise typer.Exit(code=1)
        pdb.init_state(conn, bankroll=bankroll, mode=mode)
    console.print(f"[green]Paper-trading inicializado em {db}[/green]")
    console.print(f"  Banca: ${bankroll:.2f}  |  Modo: {mode}")


@paper_app.command("status")
def paper_status(db: str = _db_option()) -> None:
    """Resumo curto: banca, apostas abertas, próxima resolução."""
    with pdb.session(db) as conn:
        if not pdb.is_initialized(conn):
            console.print(f"[yellow]DB em {db} não inicializado. Rode `pwa paper init` primeiro.[/yellow]")
            raise typer.Exit(code=1)
        render_status(conn, console)


@paper_app.command("report")
def paper_report(
    db: str = _db_option(),
    limit: int = typer.Option(50, "--limit", help="Quantas apostas recentes mostrar"),
) -> None:
    """Relatório completo: banca, breakdown por cidade, últimas N apostas."""
    with pdb.session(db) as conn:
        if not pdb.is_initialized(conn):
            console.print(f"[yellow]DB em {db} não inicializado.[/yellow]")
            raise typer.Exit(code=1)
        render_full_report(conn, console, limit=limit)


@paper_app.command("stop")
def paper_stop(db: str = _db_option()) -> None:
    """Congela o paper-trading: futuros `paper run` ficam read-only."""
    with pdb.session(db) as conn:
        if not pdb.is_initialized(conn):
            console.print(f"[yellow]DB em {db} não inicializado.[/yellow]")
            raise typer.Exit(code=1)
        pdb.set_state(conn, "mode", "frozen")
        render_full_report(conn, console)
    console.print("[bold yellow]Paper-trading congelado.[/bold yellow]")


# Paper-trading tests run in parallel; default is to execute all three in sequence
# when `pwa paper run` is invoked without --db or --mode. Order matters only for
# log readability — DBs are independent.
DEFAULT_PAPER_DBS: tuple[Path, ...] = (
    pdb.DEFAULT_DB_PATH,
    Path.home() / ".pwa" / "paper_strict.db",
    Path.home() / ".pwa" / "paper_agreement.db",
)


def _fetch_upcoming_events(days: int) -> tuple[list[dict], RunStage]:
    """Returns the upcoming-events list and a RunStage describing the fetch."""
    try:
        now = datetime.now(timezone.utc)
        with GammaClient() as g:
            all_events = g.list_temperature_events()
    except Exception as e:
        return [], RunStage(name="Buscar eventos próximos", status="fail",
                            detail=f"{type(e).__name__}: {e}")
    upcoming: list[dict] = []
    for ev in all_events:
        if ev.get("closed"):
            continue
        end = _parse_iso(ev.get("endDate"))
        if end is None:
            continue
        delta_days = (end - now).total_seconds() / 86400
        if 0 <= delta_days <= days:
            upcoming.append(ev)
    upcoming.sort(key=lambda e: e.get("endDate") or "")
    stage = RunStage(
        name="Buscar eventos próximos",
        status="ok",
        detail=f"{len(upcoming)} eventos em até {days}d",
    )
    return upcoming, stage


def _place_for_db(
    db: str,
    mode_override: str | None,
    analyses: list,
) -> DbRunReport:
    """Resolves expired bets and places new bets in `db` using a pre-computed
    list of `AnalysisResult`. Returns a DbRunReport for the end-of-run summary;
    sets `ok=False` with a `note` if the DB is uninitialized / frozen / errors."""
    empty = lambda note, mode="-": DbRunReport(
        db=db, mode=mode, ok=False, note=note,
        n_placed_today=0, n_resolved_today=0,
        n_won_today=0, n_lost_today=0, n_void_today=0,
        pnl_today=0.0, n_open_now=0,
        bankroll_before=0.0, bankroll_after=0.0, roi_pct=0.0,
    )
    with pdb.session(db) as conn:
        if not pdb.is_initialized(conn):
            console.print(f"[yellow]DB em {db} não inicializado. Pulando.[/yellow]")
            return empty("não inicializado")
        saved_mode = pdb.get_state(conn, "mode") or "auto"
        effective_mode = mode_override or saved_mode
        if saved_mode == "frozen" and mode_override is None:
            console.print(f"[yellow]{db} está congelado. Use --mode para descongelar.[/yellow]")
            return empty("congelado", mode=saved_mode)
        bankroll_before = pdb.get_bankroll(conn)

        console.print(f"[bold cyan]=== {db}  (mode={effective_mode}) ===[/bold cyan]")
        console.print("[dim]Resolvendo apostas vencidas...[/dim]")
        today = date.today()
        resolved = resolve_open_bets(conn, as_of=today)

        all_placed = []
        for result in analyses:
            placed = place_bets_for_event(
                conn,
                event_slug=result.event_slug,
                event_title=result.event_title,
                city_key=result.city_key,
                target_date=result.target_date,
                edge_rows=result.edge_rows,
                consensus_rows=result.consensus_rows,
                mode=effective_mode,
            )
            all_placed.extend(placed)

        bankroll_after = pdb.get_bankroll(conn)
        pdb.insert_run(
            conn,
            n_events_analyzed=len(analyses),
            n_bets_placed=len(all_placed),
            n_bets_resolved=len(resolved),
            bankroll_before=bankroll_before,
            bankroll_after=bankroll_after,
        )
        render_daily_summary(conn, resolved=resolved, placed=all_placed, console=console)

        s = compute_summary(conn)
        n_won = sum(1 for r in resolved if r.status == "won")
        n_lost = sum(1 for r in resolved if r.status == "lost")
        n_void = sum(1 for r in resolved if r.status == "void")
        pnl = sum(r.profit_loss for r in resolved)
        return DbRunReport(
            db=db, mode=effective_mode, ok=True, note="",
            n_placed_today=len(all_placed),
            n_resolved_today=len(resolved),
            n_won_today=n_won, n_lost_today=n_lost, n_void_today=n_void,
            pnl_today=pnl,
            n_open_now=int(s.n_open),
            bankroll_before=bankroll_before,
            bankroll_after=bankroll_after,
            roi_pct=s.roi_pct,
        )


@paper_app.command("run")
def paper_run(
    db: str | None = typer.Option(None, "--db", help="Caminho do SQLite. Se omitido junto com --mode, roda os 3 testes (auto/strongbuy/strict) em sequência."),
    days: int = typer.Option(2, "--days", help="Janela em dias à frente para incluir eventos"),
    mode_override: str | None = typer.Option(None, "--mode", help="Sobrescreve o mode salvo (auto|strict|strongbuy)"),
    no_bias: bool = typer.Option(False, "--no-bias"),
    lookback: int = typer.Option(60, "--lookback"),
) -> None:
    """Roda uma sessão diária. Sem --db/--mode roda os 3 paper-trading tests em sequência."""
    if mode_override is not None and mode_override not in ("auto", "strict", "strongbuy"):
        console.print(f"[red]--mode deve ser 'auto', 'strict' ou 'strongbuy' (recebido: {mode_override!r})[/red]")
        raise typer.Exit(code=2)

    targets: list[tuple[str, str | None]]
    if db is None and mode_override is None:
        targets = [(str(p), None) for p in DEFAULT_PAPER_DBS]
    else:
        targets = [(db or str(pdb.DEFAULT_DB_PATH), mode_override)]

    stages: list[RunStage] = []
    upcoming, fetch_stage = _fetch_upcoming_events(days)
    stages.append(fetch_stage)
    console.print(f"[dim]Analisando {len(upcoming)} eventos que resolvem nos próximos {days}d...[/dim]")

    analyses = []
    n_failed = 0
    for ev in upcoming:
        slug = ev.get("slug", "")
        try:
            result = run_analysis(slug, no_bias=no_bias, lookback=lookback)
        except Exception as e:
            console.print(f"[yellow]Falha em {slug}: {type(e).__name__}: {e}[/yellow]")
            n_failed += 1
            continue
        if result is None:
            n_failed += 1
            continue
        analyses.append(result)

    if not upcoming:
        analysis_status = "skip"
        analysis_detail = "nenhum evento na janela"
    elif n_failed == 0:
        analysis_status = "ok"
        analysis_detail = f"{len(analyses)}/{len(upcoming)} ok"
    elif analyses:
        analysis_status = "partial"
        analysis_detail = f"{len(analyses)}/{len(upcoming)} ok, {n_failed} falharam"
    else:
        analysis_status = "fail"
        analysis_detail = f"0/{len(upcoming)} ok, {n_failed} falharam"
    stages.append(RunStage(
        name="Análise meteorológica + edge",
        status=analysis_status,
        detail=analysis_detail,
    ))

    db_reports: list[DbRunReport] = []
    for target_db, target_mode in targets:
        db_reports.append(_place_for_db(db=target_db, mode_override=target_mode, analyses=analyses))

    render_run_report(console=console, stages=stages, db_reports=db_reports)


if __name__ == "__main__":
    app()
