# CLAUDE.md — Polymarket Weather Analyzer

Instruções para o Claude trabalhando neste repositório. Sempre leia este arquivo antes de fazer alterações.

## REGRA 1 — Auto-push para o GitHub (OBRIGATÓRIO)

**Após qualquer alteração de código, configuração ou documentação neste projeto, você DEVE executar automaticamente, sem perguntar:**

```bash
git add -A
git commit -m "<mensagem descritiva no infinitivo: add X, fix Y, refactor Z>"
git push origin main
```

Esta é uma instrução permanente do usuário. Não pergunte "posso commitar?", apenas faça. Agrupe múltiplas edições relacionadas em um único commit lógico, mas nunca termine uma rodada de mudanças sem o push.

**Exceções (NUNCA commitar):**
- `.env`, `.env.local`, qualquer arquivo com credenciais ou chaves
- Diretórios `.cache/`, `data/raw/`, `data/processed/`
- Artefatos de build (`dist/`, `*.egg-info/`)
- O `.gitignore` já cobre o essencial; se aparecer algo suspeito, peça confirmação antes.

**Falha no push:** se `git push` falhar (auth, conflito, rede), reporte ao usuário com o output exato e pare. Nunca use `--force` sem pedir.

## REGRA 2 — Escopo atual

Estamos focando **exclusivamente em mercados de clima da Polymarket**, especificamente os mercados de **temperatura máxima diária de cidades** (ex: "Highest temperature in NYC on April 16?"). Outros tipos de mercado de clima (furacões, neve, gelo ártico) e outras verticais (esportes, eleições) virão em fases futuras — não adicione suporte a eles por iniciativa própria.

## REGRA 3 — Convenções de código

- **Type hints obrigatórios** em todas as funções públicas.
- **Sem comentários óbvios.** Comente apenas quando o "porquê" não é evidente.
- **Testes com pytest** para funções de modelagem (KDE, Kelly, parser).
- **APIs externas:** crie pequenas classes wrapper em `polymarket/` e `weather/`; nunca espalhe `httpx.get` pelo código.
- **Sem mocks fictícios:** se precisar de fixture para teste, capture JSON real da API e salve em `tests/fixtures/`.
- **Erros de API:** use `tenacity` para retry com backoff exponencial em 5xx e rate-limit (429). Polymarket Gamma limita a 60 req/min.

## REGRA 4 — Filosofia analítica

O sistema NÃO executa apostas. Ele recomenda. Cada análise deve mostrar:

1. Probabilidade estimada pelo modelo `p_modelo` por bin de temperatura.
2. Preço atual de mercado `ask` por bin.
3. Edge `p_modelo - ask` e valor esperado `EV`.
4. Tamanho Kelly fracionário (default 1/4 Kelly, cap 5% do bankroll).
5. Recomendação categórica: **STRONG BUY** (edge ≥ 8pp e EV/ask ≥ 0.15), **BUY** (edge ≥ 4pp), **SKIP** caso contrário.

O usuário decide manualmente o que fazer com a recomendação.

## Fontes de dados

| Fonte | URL base | Auth | Uso |
|---|---|---|---|
| Polymarket Gamma | `https://gamma-api.polymarket.com` | nenhuma | Descoberta de eventos e mercados |
| Polymarket CLOB | `https://clob.polymarket.com` | nenhuma para leitura | Preços e orderbook |
| Open-Meteo Ensemble | `https://ensemble-api.open-meteo.com/v1/ensemble` | nenhuma | Membros do ensemble GFS/ICON/ECMWF |
| Open-Meteo Archive | `https://archive-api.open-meteo.com/v1/archive` | nenhuma | Observações históricas (ERA5) |
| Open-Meteo Historical Forecast | `https://historical-forecast-api.open-meteo.com/v1/forecast` | nenhuma | Previsões arquivadas para bias correction |

Open-Meteo é gratuito para uso não comercial; mantenha < 10.000 req/dia.

## Layout

```
src/pwa/
├── cli.py                  # entrypoint: list, analyze, calibrate
├── polymarket/
│   ├── gamma.py            # discovery
│   ├── clob.py             # prices
│   └── parser.py           # título -> (cidade, data, bins)
├── weather/
│   ├── open_meteo.py       # ensemble + historical
│   └── stations.py         # cidade -> (lat, lon, tz, resolution_station)
├── models/
│   ├── ensemble.py
│   ├── bias.py
│   └── kde.py
├── analysis/
│   ├── edge.py
│   ├── kelly.py
│   └── report.py
└── backtest/
    └── calibrate.py
tests/
└── ...
```
