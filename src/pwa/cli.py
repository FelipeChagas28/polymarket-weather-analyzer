from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import typer
from rich.console import Console
from rich.table import Table

from pwa.analysis.consensus import compute_consensus
from pwa.analysis.edge import evaluate_bin
from pwa.analysis.report import AnalysisContext, render
from pwa.backtest.calibrate import calibrate_city, summarize
from pwa.models.bias import BiasReport, apply_bias, compute_bias
from pwa.models.kde import bins_to_probs
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
console = Console()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    with GammaClient() as gamma:
        ev = gamma.get_event(event)

    title = ev.get("title", "")
    info = parse_event_title(title, end_date_iso=ev.get("endDate"))
    if info is None:
        console.print(f"[red]Não consegui parsear o título: {title!r}[/red]")
        console.print("[dim]Atualmente só suporto eventos no formato 'Highest/Lowest temperature in CITY on DATE?'.[/dim]")
        raise typer.Exit(code=2)

    station = get_station(info.city_key)
    if station is None:
        console.print(f"[red]Cidade desconhecida: {info.city_raw!r} (key={info.city_key}).[/red]")
        console.print("[dim]Adicione a estação em src/pwa/weather/stations.py e tente novamente.[/dim]")
        raise typer.Exit(code=3)

    markets = list(event_markets(ev))
    bin_pairs = parse_event_bins(markets)
    if not bin_pairs:
        console.print("[red]Nenhum bin reconhecido nos markets do evento.[/red]")
        raise typer.Exit(code=4)

    bins = [b for _, b in bin_pairs]
    unit = detect_unit(bins)
    unit_label = "°C" if unit == "C" else "°F"

    console.print(
        f"[dim]Buscando previsões em paralelo (Open-Meteo ensemble + modelos individuais + yr.no) "
        f"para {station.display_name} em {info.target_date} (unit={unit_label})...[/dim]"
    )

    def _fetch_ens() -> SourceForecast:
        return fetch_open_meteo_ensemble(station.lat, station.lon, info.target_date, station.tz, info.direction, unit=unit)

    def _fetch_per_model() -> list[SourceForecast]:
        try:
            return fetch_open_meteo_per_model(station.lat, station.lon, info.target_date, station.tz, info.direction, unit=unit)
        except Exception as e:
            console.print(f"[yellow]Open-Meteo per-model falhou ({type(e).__name__}: {e}); seguindo.[/yellow]")
            return []

    def _fetch_yr() -> SourceForecast | None:
        try:
            return fetch_yr_no(station.lat, station.lon, info.target_date, station.tz, info.direction, unit=unit)
        except Exception as e:
            console.print(f"[yellow]yr.no falhou ({type(e).__name__}: {e}); seguindo sem essa fonte.[/yellow]")
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
        console.print(f"[dim]Calculando bias correction ({lookback}d de histórico)...[/dim]")
        try:
            bias_report = compute_bias(
                station.lat, station.lon, station.tz, info.direction,
                today=date.today(), lookback_days=lookback, unit=unit,
            )
            samples = apply_bias(samples, bias_report)
        except Exception as e:
            console.print(f"[yellow]Bias correction falhou ({type(e).__name__}: {e}); seguindo sem correção.[/yellow]")
            bias_report = None

    probs = bins_to_probs(samples, bins)

    rows = []
    yes_asks: list[float | None] = []
    yes_bids: list[float | None] = []
    primary_sides: list[str] = []
    for (market, b), bp in zip(bin_pairs, probs):
        ask = best_yes_ask_from_market(market)
        bid = best_yes_bid_from_market(market)
        edge_row = evaluate_bin(b, bp.p_model, ask, bid)
        rows.append(edge_row)
        yes_asks.append(ask)
        yes_bids.append(bid)
        primary_sides.append(edge_row.side if edge_row.side_price is not None else "—")

    # Build the consensus over all available sources. We feed the *bias-corrected*
    # ensemble samples to keep the OM-ens column comparable to the primary p_model.
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

    consensus_rows = compute_consensus(consensus_sources, bins, yes_asks, yes_bids, primary_sides)

    ctx = AnalysisContext(
        event_title=title,
        event_slug=ev.get("slug", ""),
        station=station,
        ensemble=ens,
        bias=bias_report,
        unit=unit,
    )
    render(ctx, rows, consensus_rows=consensus_rows, console=console)


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


if __name__ == "__main__":
    app()
