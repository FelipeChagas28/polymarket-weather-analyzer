"""Render the cross-test paper-trading analysis as a PDF using fpdf2.

No external dependencies beyond fpdf2 (pure Python). Charts (bankroll
timeline) are drawn directly with FPDF line primitives.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from pwa.paper.analyze import (
    CalibrationBucket,
    Comparison,
    GroupStats,
    Narrative,
    SideStats,
    TestSummary,
)


# Color palette (R, G, B) for the 3 tests in charts
SERIES_COLORS = [
    (33, 102, 172),   # blue
    (178, 24, 43),    # red
    (27, 120, 55),    # green
]


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _safe(text: str) -> str:
    """Strip characters not supported by FPDF core Latin-1 fonts."""
    return text.encode("latin-1", "replace").decode("latin-1")


class PaperAnalysisPDF(FPDF):
    def __init__(self) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(left=15, top=15, right=15)
        self.alias_nb_pages()

    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "I", 9)
        self.set_text_color(120, 120, 120)
        self.cell(0, 7, _safe("Polymarket Weather Analyzer — Paper-Trading Analysis"),
                  align="L", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(200, 200, 200)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(3)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 7, _safe(f"Pagina {self.page_no()}/{{nb}}"), align="C")

    # ------------------------------------------------------------------
    # Building blocks
    # ------------------------------------------------------------------

    def add_section_title(self, title: str) -> None:
        self.set_font("Helvetica", "B", 13)
        self.set_text_color(20, 20, 20)
        self.ln(2)
        self.cell(0, 8, _safe(title), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(50, 50, 50)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(2)

    def add_subtitle(self, text: str) -> None:
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(60, 60, 60)
        self.ln(1)
        self.cell(0, 6, _safe(text), new_x="LMARGIN", new_y="NEXT")

    def add_paragraph(self, text: str, size: int = 10) -> None:
        self.set_font("Helvetica", "", size)
        self.set_text_color(40, 40, 40)
        self.set_x(self.l_margin)
        self.multi_cell(0, 5, _safe(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def add_table(
        self,
        headers: list[str],
        rows: list[list[str]],
        col_widths: list[float] | None = None,
        zebra: bool = True,
    ) -> None:
        if col_widths is None:
            usable = 180.0
            col_widths = [usable / len(headers)] * len(headers)

        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(50, 80, 120)
        self.set_text_color(255, 255, 255)
        for w, h in zip(col_widths, headers):
            self.cell(w, 6, _safe(str(h)), border=0, align="C", fill=True)
        self.ln()

        self.set_font("Helvetica", "", 9)
        self.set_text_color(20, 20, 20)
        for i, row in enumerate(rows):
            fill = zebra and (i % 2 == 0)
            self.set_fill_color(240, 244, 250) if fill else self.set_fill_color(255, 255, 255)
            for w, cell in zip(col_widths, row):
                self.cell(w, 5.5, _safe(str(cell)), border=0, align="L", fill=True)
            self.ln()
        self.ln(2)

    def add_bullets(self, items: list[str], indent: float = 4.0) -> None:
        self.set_font("Helvetica", "", 10)
        self.set_text_color(30, 30, 30)
        usable = self.w - self.l_margin - self.r_margin - indent
        for item in items:
            self.set_x(self.l_margin + indent)
            self.multi_cell(usable, 5, _safe("- " + item), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    # ------------------------------------------------------------------
    # Bankroll line chart
    # ------------------------------------------------------------------

    def add_bankroll_chart(self, series: list[tuple[str, list[tuple[str, float]], float]]) -> None:
        """series: [(name, [(date_iso, bankroll)...], initial), ...]"""
        if not series:
            self.add_paragraph("Sem dados de timeline.")
            return

        all_points: list[tuple[str, float]] = []
        for _name, pts, initial in series:
            if pts:
                all_points.extend(pts)
            all_points.append(("start", initial))
        if not all_points:
            self.add_paragraph("Sem dados de timeline.")
            return

        all_dates_sorted = sorted({p[0] for _, pts, _ in series for p in pts})
        all_values = [v for _, pts, _ in series for _, v in pts] + [init for _, _, init in series]
        y_min = min(all_values)
        y_max = max(all_values)
        if y_max - y_min < 0.5:
            y_max = y_min + 0.5

        chart_x = 25.0
        chart_y = self.get_y() + 5
        chart_w = 160.0
        chart_h = 65.0

        # Axes
        self.set_draw_color(120, 120, 120)
        self.set_line_width(0.2)
        self.line(chart_x, chart_y, chart_x, chart_y + chart_h)              # Y axis
        self.line(chart_x, chart_y + chart_h, chart_x + chart_w, chart_y + chart_h)  # X axis

        # Y ticks (5 ticks)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(80, 80, 80)
        for i in range(5):
            y_val = y_min + (y_max - y_min) * i / 4
            y_px = chart_y + chart_h - (chart_h * i / 4)
            self.set_draw_color(220, 220, 220)
            self.line(chart_x, y_px, chart_x + chart_w, y_px)
            self.set_xy(chart_x - 14, y_px - 1.8)
            self.cell(13, 4, _safe(f"${y_val:.2f}"), align="R")

        # X ticks
        if all_dates_sorted:
            n_x = min(6, len(all_dates_sorted))
            step = max(1, len(all_dates_sorted) // max(1, n_x - 1))
            tick_dates = all_dates_sorted[::step]
            if all_dates_sorted[-1] not in tick_dates:
                tick_dates.append(all_dates_sorted[-1])
            for d in tick_dates:
                idx = all_dates_sorted.index(d)
                x_px = chart_x + chart_w * (idx / max(1, len(all_dates_sorted) - 1))
                self.set_xy(x_px - 8, chart_y + chart_h + 1)
                self.cell(16, 4, _safe(d[5:]), align="C")

        # Lines + legend
        def y_to_px(v: float) -> float:
            return chart_y + chart_h - (v - y_min) / (y_max - y_min) * chart_h

        def x_to_px(date_iso: str) -> float:
            if not all_dates_sorted:
                return chart_x
            idx = all_dates_sorted.index(date_iso)
            return chart_x + chart_w * (idx / max(1, len(all_dates_sorted) - 1))

        self.set_line_width(0.5)
        legend_y = chart_y + chart_h + 8
        legend_x = chart_x
        self.set_font("Helvetica", "", 8)

        for i, (name, pts, initial) in enumerate(series):
            color = SERIES_COLORS[i % len(SERIES_COLORS)]
            self.set_draw_color(*color)
            self.set_text_color(*color)

            # baseline point: start at first date with `initial`
            if pts:
                prev_x = x_to_px(pts[0][0])
                prev_y = y_to_px(initial)
                for date_iso, value in pts:
                    curr_x = x_to_px(date_iso)
                    curr_y = y_to_px(value)
                    self.line(prev_x, prev_y, curr_x, curr_y)
                    prev_x, prev_y = curr_x, curr_y

            # Legend chip
            self.set_xy(legend_x, legend_y)
            self.set_fill_color(*color)
            self.rect(legend_x, legend_y + 1.5, 4, 2, "F")
            self.set_xy(legend_x + 5, legend_y)
            label = f"{name} (final {_money(pts[-1][1]) if pts else _money(initial)})"
            self.cell(60, 4, _safe(label))
            legend_x += 65

        self.set_text_color(20, 20, 20)
        self.set_y(chart_y + chart_h + 18)


# ---------------------------------------------------------------------------
# Public renderer
# ---------------------------------------------------------------------------


def render_pdf(
    summaries: list[TestSummary],
    comparison: Comparison,
    narratives: dict[str, Narrative],
    output_path: str | Path,
) -> Path:
    pdf = PaperAnalysisPDF()
    pdf.add_page()
    _render_cover(pdf, summaries)

    pdf.add_page()
    _render_executive(pdf, summaries, comparison)

    for s in summaries:
        pdf.add_page()
        _render_narrative_section(pdf, s, narratives.get(s.name))

    pdf.add_page()
    _render_yes_no(pdf, summaries)

    pdf.add_page()
    _render_by_city(pdf, summaries)

    pdf.add_page()
    _render_by_agreement(pdf, summaries)

    pdf.add_page()
    _render_by_recommendation(pdf, summaries)

    pdf.add_page()
    _render_bankroll_timeline(pdf, summaries)

    pdf.add_page()
    _render_calibration(pdf, summaries)

    pdf.add_page()
    _render_conclusions(pdf, summaries, comparison)

    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    return out


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_cover(pdf: PaperAnalysisPDF, summaries: list[TestSummary]) -> None:
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(20, 40, 80)
    pdf.ln(40)
    pdf.cell(0, 12, _safe("Paper-Trading: 3 Testes em Paralelo"),
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 8, _safe("Análise comparativa de estratégias"),
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.set_font("Helvetica", "", 11)
    today = datetime.now().strftime("%Y-%m-%d")
    pdf.cell(0, 6, _safe(f"Gerado em {today}"),
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(15)

    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 7, _safe("Testes incluídos:"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    for s in summaries:
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(50, 50, 100)
        pdf.cell(0, 6, _safe(f"{s.name}  ({s.mode})"),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(40, 40, 40)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 5, _safe(f"DB: {s.db_path}"), new_x="LMARGIN", new_y="NEXT")
        pdf.multi_cell(0, 5, _safe(f"Hipótese: {s.hypothesis}"), new_x="LMARGIN", new_y="NEXT")
        pdf.multi_cell(0, 5, _safe(
            f"Iniciado em {s.started_at[:10]} | Banca: {_money(s.bankroll_start)} "
            f"-> {_money(s.bankroll_current)} ({s.roi_pct:+.1f}%)"
        ), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)


def _render_executive(pdf: PaperAnalysisPDF, summaries: list[TestSummary], cmp: Comparison) -> None:
    pdf.add_section_title("1. Resumo Executivo")
    pdf.add_paragraph(
        "Tabela consolidada das três estratégias. Métricas calculadas apenas sobre "
        "apostas resolvidas (won/lost); 'void' não conta para winrate nem P/L."
    )
    rows = []
    for s in summaries:
        rows.append([
            f"{s.name} ({s.mode})",
            str(s.n_total),
            f"{s.n_won}W/{s.n_lost}L/{s.n_void}V",
            f"{s.winrate*100:.1f}%",
            f"{s.roi_pct:+.1f}%",
            f"{s.pl_resolved:+.2f}",
            str(s.n_open),
            f"{s.stake_open:.2f}",
        ])
    pdf.add_table(
        headers=["Teste", "Total", "Resolvidas", "Winrate", "ROI", "P/L res.", "Abertas", "Stake aberto"],
        rows=rows,
        col_widths=[40, 18, 26, 20, 18, 22, 18, 22],
    )
    pdf.ln(2)
    pdf.add_subtitle("Vencedores por métrica")
    pdf.add_bullets([
        f"Maior winrate: {cmp.best_winrate}",
        f"Maior ROI: {cmp.best_roi}",
        f"Maior P/L absoluto: {cmp.best_pl}",
        f"Melhor P/L em YES: {cmp.best_yes_pl}",
        f"Melhor P/L em NO: {cmp.best_no_pl}",
    ])


def _render_narrative_section(pdf: PaperAnalysisPDF, s: TestSummary, n: Narrative | None) -> None:
    pdf.add_section_title(f"{s.name} ({s.mode}) — Pontos fortes / fracos / observações / ajustes")
    pdf.add_paragraph(f"Hipótese: {s.hypothesis}")
    pdf.add_paragraph(
        f"Banca: {_money(s.bankroll_start)} -> {_money(s.bankroll_current)} "
        f"({s.roi_pct:+.1f}%) | Resolvidas: {s.n_won}W / {s.n_lost}L / {s.n_void}V | "
        f"Abertas: {s.n_open} (stake {_money(s.stake_open)})"
    )
    if n is None:
        pdf.add_paragraph("Sem dados.")
        return
    pdf.add_subtitle("Pontos fortes")
    pdf.add_bullets(n.strengths)
    pdf.add_subtitle("Pontos fracos")
    pdf.add_bullets(n.weaknesses)
    pdf.add_subtitle("Observações")
    pdf.add_bullets(n.observations)
    pdf.add_subtitle("Ajustes sugeridos")
    pdf.add_bullets(n.adjustments)


def _render_yes_no(pdf: PaperAnalysisPDF, summaries: list[TestSummary]) -> None:
    pdf.add_section_title("2. YES vs NO")
    pdf.add_paragraph(
        "Quebra entre as duas pernas. YES paga muito quando acerta (preços baixos) "
        "mas tem winrate baixo; NO paga pouco mas com winrate alto. O balanço "
        "entre as duas define o P/L final de cada estratégia."
    )
    rows: list[list[str]] = []
    for s in summaries:
        for side in ("YES", "NO"):
            ss = s.by_side[side]
            rows.append([
                s.name,
                side,
                f"{ss.n_won}/{ss.n_lost}",
                f"{ss.winrate*100:.1f}%",
                f"{ss.pl_resolved:+.2f}",
                str(ss.n_open),
                f"{ss.stake_open:.2f}",
            ])
    pdf.add_table(
        headers=["Teste", "Side", "W/L", "Winrate", "P/L res.", "Abertas", "Stake aberto"],
        rows=rows,
        col_widths=[28, 16, 22, 24, 26, 22, 24],
    )


def _render_by_city(pdf: PaperAnalysisPDF, summaries: list[TestSummary]) -> None:
    pdf.add_section_title("3. Performance por cidade")
    pdf.add_paragraph(
        "Top-5 cidades com melhor e pior P/L em cada teste (apostas resolvidas)."
    )
    for s in summaries:
        pdf.add_subtitle(f"{s.name} ({s.mode})")
        cities = [g for g in s.by_city if g.n_resolved > 0]
        if not cities:
            pdf.add_paragraph("Sem cidades com apostas resolvidas.")
            continue
        top = cities[:5]
        bot = list(reversed(cities[-5:])) if len(cities) > 5 else []
        rows = [
            [g.label, f"{g.n_won}", f"{g.n_lost}", f"{g.winrate*100:.0f}%", f"{g.pl_resolved:+.2f}", f"{g.roi*100:+.0f}%"]
            for g in top
        ]
        if bot and bot != top:
            rows.append(["...", "", "", "", "", ""])
            rows.extend(
                [g.label, f"{g.n_won}", f"{g.n_lost}", f"{g.winrate*100:.0f}%", f"{g.pl_resolved:+.2f}", f"{g.roi*100:+.0f}%"]
                for g in bot
            )
        pdf.add_table(
            headers=["Cidade", "W", "L", "Winrate", "P/L", "ROI"],
            rows=rows,
            col_widths=[40, 18, 18, 28, 28, 28],
        )


def _render_by_agreement(pdf: PaperAnalysisPDF, summaries: list[TestSummary]) -> None:
    pdf.add_section_title("4. Performance por agreement")
    pdf.add_paragraph(
        "Agreement (strong/moderate/weak) reflete a concordância entre as fontes "
        "meteorológicas no consensus gate. Teste 3 só aposta quando agreement=strong."
    )
    rows = []
    for s in summaries:
        for g in s.by_agreement:
            if g.n_resolved == 0 and g.n_open == 0:
                continue
            rows.append([
                s.name,
                g.label,
                f"{g.n_won}/{g.n_lost}",
                f"{g.winrate*100:.0f}%",
                f"{g.pl_resolved:+.2f}",
                str(g.n_open),
            ])
    pdf.add_table(
        headers=["Teste", "Agreement", "W/L", "Winrate", "P/L", "Abertas"],
        rows=rows,
        col_widths=[35, 30, 25, 25, 30, 25],
    )


def _render_by_recommendation(pdf: PaperAnalysisPDF, summaries: list[TestSummary]) -> None:
    pdf.add_section_title("5. Performance por recomendação (BUY vs STRONG BUY)")
    pdf.add_paragraph(
        "Quebra entre BUY (edge >= 4pp) e STRONG BUY (edge >= 8pp e EV/ask >= 0.15). "
        "Foco em validar a hipótese do Teste 2."
    )
    rows = []
    for s in summaries:
        for g in s.by_recommendation:
            if g.n_resolved == 0 and g.n_open == 0:
                continue
            rows.append([
                s.name,
                g.label,
                f"{g.n_won}/{g.n_lost}",
                f"{g.winrate*100:.0f}%",
                f"{g.pl_resolved:+.2f}",
                f"{g.roi*100:+.0f}%",
                str(g.n_open),
            ])
    pdf.add_table(
        headers=["Teste", "Recomendação", "W/L", "Winrate", "P/L", "ROI", "Abertas"],
        rows=rows,
        col_widths=[30, 30, 22, 22, 26, 22, 20],
    )


def _render_bankroll_timeline(pdf: PaperAnalysisPDF, summaries: list[TestSummary]) -> None:
    pdf.add_section_title("6. Drift de bankroll (temporal)")
    pdf.add_paragraph(
        "Evolução do bankroll por data de resolução. Cada série começa em "
        "$10.00 e acumula o P/L diário. Eixo X = data, eixo Y = bankroll em USD."
    )
    series = [(s.name, s.bankroll_timeline, s.bankroll_start) for s in summaries]
    pdf.add_bankroll_chart(series)


def _render_calibration(pdf: PaperAnalysisPDF, summaries: list[TestSummary]) -> None:
    pdf.add_section_title("7. Calibração: p_consenso vs taxa de acerto")
    pdf.add_paragraph(
        "Apostas agrupadas em buckets de 10pp de p_consenso. Se o modelo está "
        "bem calibrado, a coluna 'Hit rate' deve estar próxima do meio do bucket."
    )
    for s in summaries:
        pdf.add_subtitle(f"{s.name} ({s.mode})")
        rows = []
        for b in s.calibration:
            mid = (b.p_lo + b.p_hi) / 2.0
            diff = b.hit_rate - mid
            rows.append([
                f"{b.p_lo*100:.0f}–{b.p_hi*100:.0f}%",
                str(b.n),
                str(b.n_won),
                f"{b.hit_rate*100:.0f}%" if b.n > 0 else "-",
                f"{diff*100:+.0f}pp" if b.n > 0 else "-",
            ])
        pdf.add_table(
            headers=["Faixa de p", "n", "won", "Hit rate", "Diff vs meio"],
            rows=rows,
            col_widths=[35, 25, 25, 35, 40],
        )


def _render_conclusions(pdf: PaperAnalysisPDF, summaries: list[TestSummary], cmp: Comparison) -> None:
    pdf.add_section_title("8. Conclusões e próximos ajustes")
    if not summaries:
        pdf.add_paragraph("Sem dados.")
        return

    leader = max(summaries, key=lambda s: s.pl_resolved)
    worst = min(summaries, key=lambda s: s.pl_resolved)

    pdf.add_subtitle("Leitura cruzada")
    pdf.add_bullets([
        f"Estratégia com melhor P/L resolvido: {leader.name} ({leader.pl_resolved:+.2f}).",
        f"Estratégia com pior P/L resolvido: {worst.name} ({worst.pl_resolved:+.2f}).",
        f"Vencedor de YES: {cmp.best_yes_pl}; vencedor de NO: {cmp.best_no_pl}.",
    ])

    pdf.add_subtitle("Próximos ajustes recomendados")
    cross_bullets: list[str] = []
    yes_bleeders = [s for s in summaries if s.by_side["YES"].pl_resolved < -1.0]
    if yes_bleeders:
        names = ", ".join(s.name for s in yes_bleeders)
        cross_bullets.append(
            f"YES está sangrando em: {names}. Considere subir o piso de preço mínimo "
            "(ex: ignorar bets YES com price_entry < 0.05) ou apertar a regra de Kelly."
        )
    high_no_low_pl = [
        s for s in summaries
        if s.by_side["NO"].winrate >= 0.80 and s.by_side["NO"].pl_resolved <= 0
    ]
    if high_no_low_pl:
        names = ", ".join(s.name for s in high_no_low_pl)
        cross_bullets.append(
            f"NO com winrate alto mas P/L plano em: {names}. Reduzir stake quando "
            "price_entry > 0.85 — payout não compensa a cauda."
        )
    small_samples = [s for s in summaries if s.n_won + s.n_lost < 20]
    if small_samples:
        names = ", ".join(s.name for s in small_samples)
        cross_bullets.append(
            f"Amostra pequena em: {names}. Manter rodadas diárias por mais 2-4 semanas "
            "antes de tirar conclusão definitiva."
        )
    if not cross_bullets:
        cross_bullets.append(
            "Nenhum ajuste cross-estratégia óbvio. Continuar as 3 rodadas em paralelo."
        )
    pdf.add_bullets(cross_bullets)
