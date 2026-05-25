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
├── backtest/
│   └── calibrate.py
└── paper/
    ├── db.py               # SQLite schema + CRUD
    ├── engine.py           # place_bets, resolve_open_bets, summary
    └── report.py           # rich tables (daily summary + full report)
tests/
└── ...
```

## Paper-trading mode

O usuário valida a estratégia em paper-trading antes de operar com dinheiro real. Comandos:

```bash
pwa paper init --bankroll 10      # cria ~/.pwa/paper.db com banca de $10
pwa paper run                     # rotina diária: resolve apostas vencidas + analisa mercados ativos + coloca novas apostas + balanço
pwa paper status                  # resumo curto
pwa paper report                  # relatório completo (P/L por cidade, últimas N apostas)
pwa paper stop                    # congela o experimento
```

O DB fica em `~/.pwa/paper.db` (fora do repo). Cada aposta guarda preço de entrada, stake, p_consenso, agreement, e na resolução guarda o `realized_bin` (mesmo se a aposta tiver dado LOSS — assim dá pra ver o quão longe a recomendação ficou).

### Modos de aposta (`--mode`)

| Modo | Filtro | Uso |
|---|---|---|
| `auto` | toda recomendação BUY/STRONG BUY que passa o consensus gate | teste 1 (default) |
| `strict` | só quando `agreement == strong` (descarta moderate/weak) | alternativa não usada |
| `strongbuy` | só recomendação `STRONG BUY` (edge ≥ 8pp e EV/ask ≥ 0.15) | teste 2 |

### Testes em andamento (paper-trading)

Rodam **em paralelo**, cada um com banca e DB próprios e isolados — os três podem conter apostas iguais. Testes 1 e 2 iniciados em 2026-05-20, Teste 3 iniciado em 2026-05-24, banca $10 cada.

Para executar a rotina diária de todos os 3 testes de uma vez:

```bash
pwa paper run
```

Sem flags, o comando: (a) descobre eventos uma única vez, (b) roda `run_analysis` uma única vez por evento (cache compartilhado entre DBs) e (c) chama resolve+place_bets nos 3 DBs em sequência, cada um aplicando seu próprio modo salvo. Para rodar só um DB específico, passe `--db` ou `--mode` explicitamente.

| Teste | DB | Modo | Hipótese |
|---|---|---|---|
| **Teste 1** | `~/.pwa/paper.db` | `auto` (rede ampla) | baseline |
| **Teste 2** | `~/.pwa/paper_strict.db` | `strongbuy` (filtra por magnitude do edge) | concentrar nas de maior convicção rende ROI/winrate melhor |
| **Teste 3** | `~/.pwa/paper_agreement.db` | `strict` (filtra por agreement=strong) | apostar só quando os modelos meteorológicos concordam fortemente rende ROI/winrate melhor (eixo ortogonal ao Teste 2: convicção vem da concordância, não da magnitude) |

Comparar winrate/ROI dos três DBs após 30+ dias.

