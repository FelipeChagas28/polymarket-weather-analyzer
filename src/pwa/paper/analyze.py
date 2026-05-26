"""Cross-DB aggregation for `pwa paper analyze`.

Reads multiple paper-trading DBs and produces dimensional summaries
(YES/NO, city, agreement, recommendation, bin), bankroll timeline,
calibration buckets and narrative bullets (strengths / weaknesses /
observations / adjustments) used by `pdf_report.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pwa.paper import db as pdb
from pwa.paper.db import Bet


# ---------------------------------------------------------------------------
# Test definition + data structures
# ---------------------------------------------------------------------------

DEFAULT_TESTS: tuple[tuple[str, Path, str, str], ...] = (
    (
        "Teste 1",
        Path.home() / ".pwa" / "paper.db",
        "auto",
        "Baseline — toda BUY/STRONG BUY que passa o consensus gate.",
    ),
    (
        "Teste 2",
        Path.home() / ".pwa" / "paper_strict.db",
        "strongbuy",
        "Só STRONG BUY (edge ≥ 8pp e EV/ask ≥ 0.15). Convicção pela magnitude.",
    ),
    (
        "Teste 3",
        Path.home() / ".pwa" / "paper_agreement.db",
        "strict",
        "Só agreement=strong. Convicção pelo consenso entre fontes meteorológicas.",
    ),
)


@dataclass(frozen=True, slots=True)
class SideStats:
    n_won: int
    n_lost: int
    n_open: int
    stake_open: float
    pl_resolved: float

    @property
    def n_resolved(self) -> int:
        return self.n_won + self.n_lost

    @property
    def winrate(self) -> float:
        return self.n_won / self.n_resolved if self.n_resolved > 0 else 0.0


@dataclass(frozen=True, slots=True)
class GroupStats:
    """Per-group stats keyed by an arbitrary label (city, agreement, etc.)."""
    label: str
    n_won: int
    n_lost: int
    n_open: int
    pl_resolved: float
    stake_resolved: float

    @property
    def n_resolved(self) -> int:
        return self.n_won + self.n_lost

    @property
    def winrate(self) -> float:
        return self.n_won / self.n_resolved if self.n_resolved > 0 else 0.0

    @property
    def roi(self) -> float:
        return self.pl_resolved / self.stake_resolved if self.stake_resolved > 0 else 0.0


@dataclass(frozen=True, slots=True)
class CalibrationBucket:
    p_lo: float
    p_hi: float
    n: int
    n_won: int

    @property
    def hit_rate(self) -> float:
        return self.n_won / self.n if self.n > 0 else 0.0


@dataclass(frozen=True, slots=True)
class TestSummary:
    name: str
    db_path: str
    mode: str
    hypothesis: str
    bankroll_start: float
    bankroll_current: float
    started_at: str
    n_total: int
    n_open: int
    n_won: int
    n_lost: int
    n_void: int
    pl_resolved: float
    stake_resolved: float
    stake_open: float
    by_side: dict[str, SideStats]              # "YES" | "NO" -> SideStats
    by_city: list[GroupStats]                  # sorted by pl_resolved DESC
    by_agreement: list[GroupStats]
    by_recommendation: list[GroupStats]
    bankroll_timeline: list[tuple[str, float]]
    calibration: list[CalibrationBucket]
    consecutive_loss_bins: list[tuple[str, str, int]]  # (city, bin, streak)

    @property
    def roi_pct(self) -> float:
        if self.bankroll_start <= 0:
            return 0.0
        return (self.bankroll_current - self.bankroll_start) / self.bankroll_start * 100.0

    @property
    def winrate(self) -> float:
        denom = self.n_won + self.n_lost
        return self.n_won / denom if denom > 0 else 0.0


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_bets(db_path: str | Path) -> list[Bet]:
    """Reads all bets from a paper-trading DB ordered by placed_at ASC."""
    path = Path(db_path).expanduser()
    if not path.exists():
        return []
    with pdb.session(path) as conn:
        rows = conn.execute("SELECT * FROM bets ORDER BY placed_at ASC").fetchall()
        return [pdb._row_to_bet(r) for r in rows]


def _load_state(db_path: str | Path) -> dict[str, str | None]:
    path = Path(db_path).expanduser()
    if not path.exists():
        return {}
    with pdb.session(path) as conn:
        return {
            "bankroll_start": pdb.get_state(conn, "bankroll_start"),
            "bankroll_current": pdb.get_state(conn, "bankroll_current"),
            "started_at": pdb.get_state(conn, "started_at"),
            "mode": pdb.get_state(conn, "mode"),
        }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _group_by(bets: list[Bet], key) -> list[GroupStats]:
    buckets: dict[str, dict] = {}
    for b in bets:
        k = key(b) or "unknown"
        d = buckets.setdefault(k, {"won": 0, "lost": 0, "open": 0, "pl": 0.0, "stake_resolved": 0.0})
        if b.status == "won":
            d["won"] += 1
            d["pl"] += b.profit_loss or 0.0
            d["stake_resolved"] += b.stake
        elif b.status == "lost":
            d["lost"] += 1
            d["pl"] += b.profit_loss or 0.0
            d["stake_resolved"] += b.stake
        elif b.status == "open":
            d["open"] += 1
    out = [
        GroupStats(
            label=label,
            n_won=d["won"],
            n_lost=d["lost"],
            n_open=d["open"],
            pl_resolved=round(d["pl"], 4),
            stake_resolved=round(d["stake_resolved"], 4),
        )
        for label, d in buckets.items()
    ]
    out.sort(key=lambda g: g.pl_resolved, reverse=True)
    return out


def _side_stats(bets: list[Bet], side: str) -> SideStats:
    n_won = n_lost = n_open = 0
    pl = 0.0
    stake_open = 0.0
    for b in bets:
        if b.side != side:
            continue
        if b.status == "won":
            n_won += 1
            pl += b.profit_loss or 0.0
        elif b.status == "lost":
            n_lost += 1
            pl += b.profit_loss or 0.0
        elif b.status == "open":
            n_open += 1
            stake_open += b.stake
    return SideStats(
        n_won=n_won, n_lost=n_lost, n_open=n_open,
        stake_open=round(stake_open, 4), pl_resolved=round(pl, 4),
    )


def bankroll_timeline(bets: list[Bet], initial: float) -> list[tuple[str, float]]:
    """Cumulative bankroll per resolution date. Always starts with (started_at, initial)."""
    resolved = sorted(
        (b for b in bets if b.status in ("won", "lost", "void") and b.resolved_at),
        key=lambda b: b.resolved_at or "",
    )
    timeline: list[tuple[str, float]] = []
    bank = initial
    per_day: dict[str, float] = {}
    for b in resolved:
        day = (b.resolved_at or "")[:10]
        per_day[day] = per_day.get(day, 0.0) + (b.profit_loss or 0.0)
    for day in sorted(per_day.keys()):
        bank += per_day[day]
        timeline.append((day, round(bank, 4)))
    return timeline


def calibration_buckets(bets: list[Bet], n_buckets: int = 10) -> list[CalibrationBucket]:
    """Buckets by `p_consenso` (0–0.1, 0.1–0.2, …) with realized hit rate."""
    step = 1.0 / n_buckets
    counts: list[tuple[int, int]] = [(0, 0) for _ in range(n_buckets)]
    for b in bets:
        if b.status not in ("won", "lost"):
            continue
        p = b.p_consenso
        idx = min(int(p / step), n_buckets - 1)
        n, w = counts[idx]
        counts[idx] = (n + 1, w + (1 if b.status == "won" else 0))
    return [
        CalibrationBucket(p_lo=i * step, p_hi=(i + 1) * step, n=n, n_won=w)
        for i, (n, w) in enumerate(counts)
    ]


def _consecutive_losses(bets: list[Bet], min_streak: int = 3) -> list[tuple[str, str, int]]:
    """Bins (city, bin_label) with min_streak+ consecutive losses (chronological)."""
    by_bin: dict[tuple[str, str], list[Bet]] = {}
    for b in bets:
        if b.status not in ("won", "lost"):
            continue
        by_bin.setdefault((b.city_key, b.bin_label), []).append(b)

    out: list[tuple[str, str, int]] = []
    for (city, label), items in by_bin.items():
        items.sort(key=lambda b: b.resolved_at or "")
        streak = 0
        max_streak = 0
        for b in items:
            if b.status == "lost":
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        if max_streak >= min_streak:
            out.append((city, label, max_streak))
    out.sort(key=lambda r: r[2], reverse=True)
    return out


def summarize_test(
    name: str,
    db_path: str | Path,
    mode_label: str,
    hypothesis: str,
) -> TestSummary | None:
    bets = load_bets(db_path)
    state = _load_state(db_path)
    if not state:
        return None
    bankroll_start = float(state.get("bankroll_start") or 0.0)
    bankroll_current = float(state.get("bankroll_current") or 0.0)
    started_at = state.get("started_at") or "-"

    counts = {"won": 0, "lost": 0, "void": 0, "open": 0}
    pl_resolved = 0.0
    stake_resolved = 0.0
    stake_open = 0.0
    for b in bets:
        counts[b.status] = counts.get(b.status, 0) + 1
        if b.status in ("won", "lost"):
            pl_resolved += b.profit_loss or 0.0
            stake_resolved += b.stake
        elif b.status == "open":
            stake_open += b.stake

    return TestSummary(
        name=name,
        db_path=str(db_path),
        mode=mode_label,
        hypothesis=hypothesis,
        bankroll_start=bankroll_start,
        bankroll_current=bankroll_current,
        started_at=started_at,
        n_total=len(bets),
        n_open=counts.get("open", 0),
        n_won=counts.get("won", 0),
        n_lost=counts.get("lost", 0),
        n_void=counts.get("void", 0),
        pl_resolved=round(pl_resolved, 4),
        stake_resolved=round(stake_resolved, 4),
        stake_open=round(stake_open, 4),
        by_side={
            "YES": _side_stats(bets, "YES"),
            "NO": _side_stats(bets, "NO"),
        },
        by_city=_group_by(bets, lambda b: b.city_key),
        by_agreement=_group_by(bets, lambda b: b.agreement),
        by_recommendation=_group_by(bets, lambda b: b.recommendation),
        bankroll_timeline=bankroll_timeline(bets, initial=bankroll_start),
        calibration=calibration_buckets(bets),
        consecutive_loss_bins=_consecutive_losses(bets),
    )


def summarize_all(tests=DEFAULT_TESTS) -> list[TestSummary]:
    out: list[TestSummary] = []
    for name, path, mode, hyp in tests:
        s = summarize_test(name, path, mode, hyp)
        if s is not None:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Cross-test comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Comparison:
    best_winrate: str
    best_roi: str
    best_pl: str
    best_yes_pl: str
    best_no_pl: str


def compare_tests(summaries: list[TestSummary]) -> Comparison:
    def best(metric) -> str:
        if not summaries:
            return "-"
        return max(summaries, key=metric).name

    return Comparison(
        best_winrate=best(lambda s: s.winrate),
        best_roi=best(lambda s: s.roi_pct),
        best_pl=best(lambda s: s.pl_resolved),
        best_yes_pl=best(lambda s: s.by_side["YES"].pl_resolved),
        best_no_pl=best(lambda s: s.by_side["NO"].pl_resolved),
    )


# ---------------------------------------------------------------------------
# Narrative blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Narrative:
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)


def _narrative_for(s: TestSummary) -> Narrative:
    strengths: list[str] = []
    weaknesses: list[str] = []
    observations: list[str] = []
    adjustments: list[str] = []

    yes = s.by_side["YES"]
    no = s.by_side["NO"]
    n_resolved = s.n_won + s.n_lost

    # --- Strengths ---
    if no.n_resolved >= 5 and no.winrate >= 0.80:
        strengths.append(
            f"NO defensivas com winrate alto ({no.winrate*100:.1f}% em {no.n_resolved} apostas) — "
            "perna estável do P/L."
        )
    if s.roi_pct > 5:
        strengths.append(f"ROI positivo ({s.roi_pct:+.1f}%) com {n_resolved} apostas resolvidas.")
    if yes.n_resolved >= 5 and yes.winrate >= 0.25:
        strengths.append(
            f"YES com winrate decente ({yes.winrate*100:.1f}%) — o filtro do modo {s.mode} "
            "está selecionando bets razoáveis."
        )
    if s.by_city and s.by_city[0].pl_resolved > 0.5:
        top = s.by_city[0]
        strengths.append(
            f"Melhor cidade: {top.label} (P/L {top.pl_resolved:+.2f}, "
            f"{top.n_won}W/{top.n_lost}L)."
        )

    # --- Weaknesses ---
    if yes.pl_resolved < -1.0:
        strengths_kept = yes.n_won
        weaknesses.append(
            f"YES está sangrando ({yes.pl_resolved:+.2f} em {yes.n_resolved} apostas, "
            f"winrate {yes.winrate*100:.1f}%, apenas {strengths_kept} acertos). "
            "Muitos tickets baratos perdendo."
        )
    if s.roi_pct < -10:
        weaknesses.append(
            f"ROI em {s.roi_pct:+.1f}% — bankroll vem caindo desde {s.started_at[:10]}."
        )
    if no.pl_resolved < -0.5 and no.n_resolved >= 5:
        weaknesses.append(
            f"NO performance ruim ({no.pl_resolved:+.2f} em {no.n_resolved}) — "
            "preços altos não estão compensando os losses."
        )
    if s.by_city and s.by_city[-1].pl_resolved < -0.5:
        bot = s.by_city[-1]
        weaknesses.append(
            f"Pior cidade: {bot.label} (P/L {bot.pl_resolved:+.2f}, "
            f"{bot.n_won}W/{bot.n_lost}L) — considerar excluir do universo."
        )

    # --- Observations ---
    if n_resolved < 20:
        observations.append(
            f"Amostra pequena ({n_resolved} apostas resolvidas) — conclusões preliminares."
        )
    exposure = s.stake_open / s.bankroll_current if s.bankroll_current > 0 else 0
    if exposure > 0.4:
        observations.append(
            f"Exposição alta em apostas abertas: ${s.stake_open:.2f} "
            f"({exposure*100:.0f}% do bankroll atual de ${s.bankroll_current:.2f})."
        )
    # Agreement distribution
    for g in s.by_agreement:
        if g.n_resolved >= 5:
            observations.append(
                f"Agreement={g.label}: {g.n_won}W/{g.n_lost}L "
                f"(winrate {g.winrate*100:.0f}%, P/L {g.pl_resolved:+.2f})."
            )

    # Calibration — over/under confidence
    over_conf = []
    under_conf = []
    for b in s.calibration:
        if b.n < 5:
            continue
        midpoint = (b.p_lo + b.p_hi) / 2.0
        if midpoint - b.hit_rate > 0.15:
            over_conf.append(b)
        elif b.hit_rate - midpoint > 0.15:
            under_conf.append(b)
    if over_conf:
        ranges = ", ".join(f"{b.p_lo*100:.0f}–{b.p_hi*100:.0f}%" for b in over_conf)
        observations.append(
            f"Modelo super-confiante nas faixas {ranges}: p_consenso > taxa de acerto em >=15pp."
        )
    if under_conf:
        ranges = ", ".join(f"{b.p_lo*100:.0f}–{b.p_hi*100:.0f}%" for b in under_conf)
        observations.append(
            f"Modelo sub-confiante nas faixas {ranges}: taxa de acerto > p_consenso em >=15pp."
        )

    # --- Adjustments ---
    if s.consecutive_loss_bins:
        top3 = s.consecutive_loss_bins[:3]
        joined = "; ".join(f"{c}/{lbl} ({n}x)" for c, lbl, n in top3)
        adjustments.append(
            f"Bins com losses consecutivos >=3: {joined}. Considerar regra de cooldown."
        )

    # STRONG BUY vs BUY winrate
    rec_wr = {g.label: g.winrate for g in s.by_recommendation if g.n_resolved >= 3}
    if "STRONG BUY" in rec_wr and "BUY" in rec_wr:
        if rec_wr["STRONG BUY"] < rec_wr["BUY"]:
            adjustments.append(
                f"STRONG BUY ({rec_wr['STRONG BUY']*100:.0f}%) performando pior que "
                f"BUY ({rec_wr['BUY']*100:.0f}%) — revisar threshold de edge/EV."
            )

    if yes.pl_resolved < -1.0 and yes.n_resolved >= 10:
        adjustments.append(
            "YES sangrando consistentemente — sugestão: subir piso de preço mínimo "
            "para YES (ex: ignorar bets com price_entry < 0.05)."
        )

    if no.n_resolved >= 10 and no.winrate >= 0.80 and no.pl_resolved < 0:
        adjustments.append(
            "NO ganha em volume mas perde em P/L (preços altos): reduzir stake "
            "quando price_entry > 0.85 — payout não compensa o risco de cauda."
        )

    if not strengths:
        strengths.append("Nenhum ponto forte estatisticamente notável ainda.")
    if not weaknesses:
        weaknesses.append("Nenhum ponto fraco estatisticamente notável.")
    if not observations:
        observations.append("Sem observações relevantes além das métricas básicas.")
    if not adjustments:
        adjustments.append("Continuar o teste sem ajustes — sinal insuficiente para mudar.")

    return Narrative(
        strengths=strengths,
        weaknesses=weaknesses,
        observations=observations,
        adjustments=adjustments,
    )


def narrative_blocks(summaries: list[TestSummary]) -> dict[str, Narrative]:
    return {s.name: _narrative_for(s) for s in summaries}
