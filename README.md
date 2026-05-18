# Polymarket Weather Analyzer (`pwa`)

CLI em Python que analisa mercados de **temperatura diária de cidades** na Polymarket e calcula edge estatístico contra previsões de ensemble meteorológico (GFS + ICON + ECMWF via Open-Meteo).

> ⚠️ O sistema **não executa apostas**. Ele recomenda; você decide.

## Como funciona

1. Lista eventos ativos de temperatura na Polymarket (`tag=climate`, janela ≤ 7 dias).
2. Para o evento escolhido, busca previsão **ensemble** (~50-90 membros) da temperatura máxima do dia na cidade-alvo via Open-Meteo.
3. Aplica correção de viés a partir do histórico recente (forecast vs observado).
4. Estima a distribuição via KDE e calcula `P(bin)` integrando a densidade.
5. Compara com o `ask` de cada bin no CLOB da Polymarket, calcula edge, EV e tamanho Kelly fracionário.
6. Exibe relatório `rich` com recomendação por bin: **STRONG BUY / BUY / SKIP**.

## Instalação

```bash
pip install -e .
```

## Uso

```bash
pwa list                          # mercados de temperatura ativos
pwa analyze <event-slug-or-id>    # análise estatística do evento
pwa calibrate nyc --n 30          # Brier/log-loss em mercados passados (validação)
```

## Stack

`httpx` · `pydantic` · `typer` · `rich` · `numpy` · `scipy` · `pandas`

## APIs públicas usadas

- Polymarket Gamma + CLOB (read-only, sem auth)
- Open-Meteo Ensemble + Archive + Historical Forecast (sem chave; uso não comercial)
