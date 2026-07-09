# Crypto Kraken Telegram Bot

Bot Python pronto per Railway che analizza OHLC Kraken, genera segnali **LONG spot**, invia messaggi Telegram, monitora **Target 1 / Stop Loss**, salva tutto in SQLite e produce report giornalieri.

> Nota: il bot non usa TP2 e non mostra risultati in R. Tutti i risultati operativi sono in euro.

## File del progetto

- `app.py` — entrypoint Railway (`python app.py`) e loop worker.
- `config.py` — configurazione da variabili ambiente.
- `exchange.py` — client Kraken OHLC pubblico con retry/rate limit.
- `indicators.py` — EMA, RSI, MACD, ATR, ADX, volumi, supporti/resistenze e regime mercato.
- `strategy.py` — logica LONG per `CONSERVATIVE` e `SCALPING_FAST`.
- `risk.py` — TP1/SL dinamici e calcoli netti in euro.
- `scoring.py` — score 0-100 e decisione `OPERATIVE`, `WATCHLIST`, `REJECTED`.
- `trade_store.py` — database SQLite `signals.db` e tabella `signals`.
- `monitor.py` — controllo trade aperti con OHLC reali Kraken.
- `reporter.py` — report giornaliero e analisi automatica storico.
- `backtest.py` — backtest leggero, senza invio segnali reali.
- `telegram_bot.py` — invio messaggi Telegram.
- `requirements.txt` — dipendenze Python.
- `Procfile` — Railway worker.



## Project Alpha

Project Alpha è la nuova modalità di ricerca basata su price action, market structure e liquidità. Quando `PROJECT_ALPHA_RESEARCH_MODE=true`, il bot **non invia segnali live** e genera soltanto file di ricerca:

- `alpha_research_report.csv`;
- `alpha_research_summary.txt`.

Strategie testate in `research_alpha.py`:

1. Liquidity Sweep Long;
2. Breakout Retest Long;
3. Pullback Trend Long;
4. Range Reversal Long.

Il modulo costruisce una market map con swing high/low, supporti, resistenze, liquidity pool, range, volatilità ATR e volume relativo. Gli indicatori non sono usati come trigger principali: ATR, ADX, volume relativo ed EMA servono solo come filtri/contesto.

Criteri di validazione Project Alpha:

- almeno 100 trade nel training;
- profit factor training > 1.20;
- win rate training > 52%;
- expectancy training positiva;
- validation profittevole;
- profit factor validation > 1.10.

Se nessuna strategia supera questi criteri, il summary scrive:

```text
NESSUN EDGE VALIDATO
```

Comando manuale per eseguire solo la ricerca Alpha:

```bash
python research_alpha.py
```

Per sicurezza, `ENABLE_LIVE_SIGNALS=false` impedisce la riattivazione accidentale dei segnali operativi.

## Research Mode

La vecchia modalità `RESEARCH_MODE` rimane disponibile ma non è più la modalità principale: Project Alpha usa `PROJECT_ALPHA_RESEARCH_MODE=true` e `RESEARCH_MODE=false`. Se in futuro vuoi eseguire la ricerca precedente basata su combinazioni più classiche, puoi impostare `PROJECT_ALPHA_RESEARCH_MODE=false` e `RESEARCH_MODE=true`.

Criteri minimi per dichiarare edge statistico:

- almeno `RESEARCH_MIN_TRADES`, default 200 trade;
- win rate TP1 > 55%;
- profit factor > 1.25;
- expectancy positiva;
- validation set ancora profittevole.

Se nessuna configurazione supera i criteri, il report scrive chiaramente:

```text
NESSUN EDGE STATISTICO TROVATO
```

Strategie testate:

1. Breakout trend-following;
2. Pullback su EMA20/EMA50;
3. Mean reversion in mercato laterale;
4. Momentum dopo volume spike;
5. Continuation dopo candela forte;
6. Momentum filtrato per evitare trade contro trend BTC.

Variabili principali:

```env
PROJECT_ALPHA_RESEARCH_MODE=true
ENABLE_LIVE_SIGNALS=false
RESEARCH_MODE=false
RESEARCH_OHLC_LIMIT=720
RESEARCH_MIN_TRADES=200
RESEARCH_TIMEFRAMES=5m,15m,1h
RESEARCH_INTERVAL_SECONDS=86400
ALPHA_PAIRS=BTC/USD,ETH/USD,SOL/USD,LINK/USD,UNI/USD,AAVE/USD,AVAX/USD,XRP/USD
ALPHA_TIMEFRAMES=5m,15m,1h
ALPHA_REPORT_CSV=alpha_research_report.csv
ALPHA_REPORT_SUMMARY=alpha_research_summary.txt
```

Per riattivare i segnali live in futuro servono entrambe le condizioni: `PROJECT_ALPHA_RESEARCH_MODE=false` e `ENABLE_LIVE_SIGNALS=true`, ma solo dopo aver validato una strategia con edge statistico.

## Modalità

La modalità predefinita è `CONSERVATIVE`. Per tornare allo scalping veloce, imposta `MODE=SCALPING_FAST` su Railway.

### SCALPING_FAST

- Timeframe principale: `5m`
- Conferma 1: `15m`
- Conferma 2: `1h`
- Score operativo minimo: `68`
- Watchlist: `60`
- Limiti: max 30 segnali/giorno, max 3 per coppia/giorno, no duplicati entro 60 minuti.
- Con `ENFORCE_SETUP_RULES=false`, un setup con score operativo e piano RR valido resta operativo anche se manca una regola secondaria; le regole mancanti rimangono nei log come penalità.

### CONSERVATIVE

- Timeframe principale: `15m`
- Conferma 1: `1h`
- Conferma 2: `4h`
- Score operativo minimo: `85`
- Watchlist: `70`

## Variabili ambiente Railway

Imposta queste variabili nella sezione **Variables** del servizio Railway:

```env
TELEGRAM_BOT_TOKEN=123456789:token_del_bot
TELEGRAM_CHAT_ID=123456789
# Opzionale: username gruppo/canale pubblico senza @ per link cliccabili
TELEGRAM_CHAT_USERNAME=
PROJECT_ALPHA_RESEARCH_MODE=true
ENABLE_LIVE_SIGNALS=false
RESEARCH_MODE=false
RESEARCH_OHLC_LIMIT=720
RESEARCH_MIN_TRADES=200
RESEARCH_TIMEFRAMES=5m,15m,1h
RESEARCH_INTERVAL_SECONDS=86400
ALPHA_PAIRS=BTC/USD,ETH/USD,SOL/USD,LINK/USD,UNI/USD,AAVE/USD,AVAX/USD,XRP/USD
ALPHA_TIMEFRAMES=5m,15m,1h
ALPHA_REPORT_CSV=alpha_research_report.csv
ALPHA_REPORT_SUMMARY=alpha_research_summary.txt
MODE=CONSERVATIVE
TRADE_AMOUNT_EUR=100.0
FEE_BUY_PERCENT=0.10
FEE_SELL_PERCENT=0.10
SLIPPAGE_PERCENT=0.00
SPREAD_PERCENT=0.00
MIN_SIGNAL_SCORE_CONSERVATIVE=85
MIN_WATCHLIST_SCORE_CONSERVATIVE=70
MIN_SIGNAL_SCORE_SCALPING=68
MIN_WATCHLIST_SCORE_SCALPING=60
MIN_NET_RR=1.05
ENABLE_WATCHLIST_ALERTS=false
ENFORCE_SETUP_RULES=false
DAILY_REPORT_HOUR=9
LOOP_SLEEP_SECONDS=300
OHLC_LIMIT=300
DATABASE_PATH=signals.db
PAIRS=BTC/USD,ETH/USD,SOL/USD,LINK/USD,UNI/USD,AAVE/USD,LTC/USD,BCH/USD,AVAX/USD,TAO/USD,XRP/USD,ADA/USD,DOGE/USD,DOT/USD,XLM/USD,TRX/USD,ATOM/USD,ETC/USD,FIL/USD,NEAR/USD
MAX_SIGNALS_PER_DAY=30
MAX_SIGNALS_PER_PAIR_PER_DAY=3
DUPLICATE_MINUTES=60
MAX_CONSECUTIVE_STOP_LOSSES=3
STOP_LOSS_PAUSE_HOURS=12
```

## Avvio locale

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Su Railway il comando è già nel `Procfile`:

```bash
worker: python app.py
```

## Test Telegram

Puoi testare Telegram con:

```bash
python - <<'PY'
from telegram_bot import test_telegram
print(test_telegram())
PY
```

Se ricevi `False`, controlla:

1. `TELEGRAM_BOT_TOKEN` corretto.
2. `TELEGRAM_CHAT_ID` corretto.
3. Hai scritto almeno un messaggio al bot prima di farlo inviare.
4. Se usi gruppo/canale, il bot deve essere aggiunto alla chat.

## Come leggere il database SQLite

Il database predefinito è `signals.db`.

Apri una shell locale e usa:

```bash
sqlite3 signals.db
.tables
.schema signals
SELECT signal_id, signal_telegram_message_id, outcome_telegram_message_id, timestamp, mode, pair, status, net_rr, realized_result_eur FROM signals ORDER BY id DESC LIMIT 20;
```

Per contare gli esiti:

```sql
SELECT mode, status, COUNT(*) FROM signals GROUP BY mode, status;
```

## Messaggi Telegram

### Segnale operativo

```text
🟢 LONG SOL/USD
ID Segnale: SIG-20260707-2015-SOLUSD-SCALPING_FAST-LONG
Modalità: SCALPING_FAST
Rischio: ALTO
Importo: €100.00

Entry: 74.1200
Stop Loss: 73.8300
Target 1: 74.5200

Profitto netto stimato TP1: +€0.3400
Perdita netta stimata SL: -€0.3900
RR netto: 1.08
Score: 72/100

Regime mercato: TREND_STRONG

Motivi:
- EMA20 > EMA50
- RSI valido
- MACD positivo
- Volume sufficiente
- BTC non ribassista
```

### Watchlist

Per impostazione predefinita, le notifiche Telegram della watchlist sono disattivate:

```env
ENABLE_WATCHLIST_ALERTS=false
```

Quindi una decisione `WATCHLIST` viene salvata nel database e mostrata nei log/report, ma **non** invia notifiche Telegram e **non** viene monitorata per TP1/SL. Riceverai notifiche Telegram solo per:

- segnali operativi `OPEN`;
- esiti `TP1` o `SL` dei trade operativi;
- report giornaliero.

Se in futuro vuoi riattivare le notifiche watchlist, imposta:

```env
ENABLE_WATCHLIST_ALERTS=true
```

Esempio messaggio watchlist quando abilitata:

```text
👀 WATCHLIST
SOL/USD
Direzione: LONG
Entry teorica: 74.1200
Score: 64/100
Motivi:
- EMA20 > EMA50
- RSI valido
Perché non operativo:
- sotto soglia operativa
```

### Target 1

```text
✅ Target 1 raggiunto
ID Segnale: SIG-20260707-2015-SOLUSD-SCALPING_FAST-LONG
LONG SOL/USD
Entry: 74.1200
Target 1: 74.5200
Risultato netto: +€0.3400
Collegamento: risposta al segnale originale
```

### Stop Loss

```text
🛑 Stop Loss raggiunto
ID Segnale: SIG-20260707-2015-SOLUSD-SCALPING_FAST-LONG
LONG SOL/USD
Entry: 74.1200
Stop Loss: 73.8300
Risultato netto: -€0.3900
Collegamento: risposta al segnale originale
```




## Entry e candele chiuse

Il bot non usa più l'ultima candela se è ancora in formazione. Prima di calcolare il segnale, filtra i dati e considera solo candele completamente chiuse per ogni timeframe (`5m`, `15m`, `1h` o `4h`). In modalità `SCALPING_FAST`, quindi, l'entry teorica deriva dalla chiusura dell'ultima candela 5m chiusa, mentre le conferme usano solo candele 15m e 1h già chiuse.

Questo evita segnali falsati da high/low/close provvisori della candela corrente.

## Protezione dopo troppi Stop Loss

Per evitare di continuare a inviare operativi in una fase negativa, il bot ora ha una pausa automatica configurabile. Se gli ultimi trade chiusi sono tutti Stop Loss e raggiungono la soglia `MAX_CONSECUTIVE_STOP_LOSSES`, i nuovi setup operativi vengono salvati come `REJECTED` per `STOP_LOSS_PAUSE_HOURS` ore.

Default consigliato:

```env
MAX_CONSECUTIVE_STOP_LOSSES=3
STOP_LOSS_PAUSE_HOURS=12
```

Inoltre, anche con `ENFORCE_SETUP_RULES=false`, alcune condizioni critiche continuano a bloccare il passaggio a operativo, per esempio trend non favorevole, RSI fuori range, BTC in forte trend ribassista o spazio insufficiente verso TP1. Le regole secondarie restano invece penalità informative.

## Collegamento TP1/SL al segnale originale

Ogni segnale operativo contiene un `ID Segnale` e, quando Telegram restituisce il `message_id`, il bot lo salva nel database SQLite. Quando arriva un messaggio TP1 o SL, il bot usa quel `message_id` come `reply_to_message_id`: su Telegram vedrai quindi l'esito come risposta diretta al segnale originale.

Se usi un gruppo o canale pubblico e imposti `TELEGRAM_CHAT_USERNAME`, il messaggio TP1/SL include anche un link cliccabile `Apri segnale originale`. Nelle chat private Telegram non espone un link pubblico stabile, quindi il collegamento avviene tramite reply e tramite lo stesso `ID Segnale`.

Nel database i campi sono:

- `signal_telegram_message_id`: ID Telegram del messaggio operativo originale;
- `outcome_telegram_message_id`: ID Telegram del messaggio TP1/SL;
- `signal_id`: ID leggibile usato sia nel segnale sia nell'esito.

## Esempio report giornaliero

```text
📊 Report giornaliero crypto
Periodo: ultime 24 ore

Modalità: CONSERVATIVE
Segnali operativi: 1
Watchlist: 2
Scartati: 5
Trade chiusi: 1
Trade aperti: 0
TP1: 1
SL: 0
Win rate TP1: 100.00%
Profit factor: 0.04
Profitto totale: €0.3400
Perdita totale: €0.0000
Saldo netto: €0.3400
Capitale iniziale: €100.00
Capitale attuale teorico: €100.340
Migliori coppie: [('SOL/USD', 0.3400)]
Peggiori coppie: [('SOL/USD', 0.3400)]
Migliori orari: [('8', 0.3400)]
Peggiori orari: [('8', 0.3400)]
Motivi principali di scarto: []

Modalità: SCALPING_FAST
...

Analisi automatica:
- Storico ancora limitato: attendere più trade prima di ottimizzare i filtri.
```

## Backtest

Il backtest non invia segnali reali:

```bash
python backtest.py
```

Metriche incluse: numero trade, win rate, profit factor, expectancy, max drawdown, sharpe ratio, profitto netto €, guadagno medio €, perdita media €, migliori/peggiori coppie.

## Deploy Railway

1. Carica i file su GitHub.
2. Crea un progetto Railway da GitHub.
3. Seleziona il repo.
4. Railway rileverà Python e userà il `Procfile`.
5. Inserisci le variabili ambiente.
6. Avvia il deploy.
7. Nei log dovresti vedere righe tipo `PAIR=SOL/USD MODE=CONSERVATIVE ...`.

## Note operative

- Il bot usa solo dati pubblici Kraken: non servono API key exchange.
- Se una pair non è disponibile su Kraken, nei log vedrai un warning e la scansione continuerà.
- SQLite su Railway è adatto a una prima versione, ma può essere resettato con redeploy/container nuovi; per storico permanente valuta un volume persistente o PostgreSQL.
- Con `ENABLE_WATCHLIST_ALERTS=false` il bot non manda notifiche watchlist: Telegram resta pulito e ricevi solo operativi, esiti e report.
- Con `ENFORCE_SETUP_RULES=false` il bot è meno restrittivo sulle penalità secondarie, ma le condizioni critiche continuano a bloccare i segnali più deboli. Per tornare a una modalità ancora più rigida, imposta `ENFORCE_SETUP_RULES=true`.
- I prezzi nei messaggi sono formattati con almeno 4 decimali, e 6 decimali per crypto sotto 1 euro/dollaro.

## Quant Research Engine e Railway Volume

Il progetto ora nasce come **Quant Research Engine**: non si limita più a generare segnali o a scrivere `NESSUN EDGE VALIDATO`, ma esegue una ricerca massiva su strategie, pair, timeframe, regimi di mercato e parametri tecnici per individuare combinazioni quasi profittevoli e aree da approfondire.

### Modalità operative

Configura `RUN_MODE`:

```env
RUN_MODE=RESEARCH
ENABLE_LIVE_SIGNALS=false
```

Valori disponibili:

- `RESEARCH` — sincronizza i dati OHLC mancanti, esegue la ricerca quantitativa in batch, salva tutti i risultati e produce report/CSV.
- `SYNC_DATA` — aggiorna solo il database OHLC locale, senza backtest e senza inviare segnali.
- `LIVE` — usa la parte live del bot; invia segnali solo se `ENABLE_LIVE_SIGNALS=True`.

Di default il progetto usa `RUN_MODE=RESEARCH` e `ENABLE_LIVE_SIGNALS=false`.

### Persistenza SQLite su Railway

Railway può perdere i file locali quando il container viene ricreato. Per conservare database e report:

1. Crea un **Railway Volume**.
2. Montalo sul path `/data`.
3. Imposta la variabile:

```env
DATA_DIR=/data
```

4. Per ricerca quantitativa:

```env
RUN_MODE=RESEARCH
```

5. Per aggiornare solo i dati storici:

```env
RUN_MODE=SYNC_DATA
```

6. Per modalità live:

```env
RUN_MODE=LIVE
ENABLE_LIVE_SIGNALS=True
```

Se il volume non è montato, il bot usa automaticamente `./data` come fallback e scrive nei log:

```text
WARNING: persistent volume not detected, data may be lost on redeploy
```

I percorsi usati sono configurati in `config.py`:

```python
DATA_DIR = os.getenv("DATA_DIR", "/data")
OHLC_DB_PATH = os.path.join(DATA_DIR, "ohlc_cache.sqlite")
RESEARCH_DB_PATH = os.path.join(DATA_DIR, "research_database.sqlite")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
```

### Database OHLC locale

Il motore usa `ohlc_cache.sqlite` per non scaricare continuamente le stesse candele da Kraken. La tabella `ohlc_data` salva:

- exchange;
- pair;
- timeframe;
- timestamp;
- open/high/low/close;
- volume;
- created_at.

È presente un indice unico su `exchange + pair + timeframe + timestamp`. Le funzioni principali sono:

- `sync_ohlc_cache(pair, timeframe, start_date, end_date)`;
- `get_ohlc_from_cache(pair, timeframe, start_date, end_date)`;
- `update_missing_ohlc(pair, timeframe)`.

Tutti i backtest del Quant Research Engine leggono dal cache SQLite e scaricano da Kraken solo quando mancano dati sufficienti.

### Ricerca massiva in batch

La ricerca combina automaticamente:

- strategie: Breakout Retest, Pullback Trend, Liquidity Sweep, Range Reversal, Momentum Breakout, Compression Breakout, Volatility Expansion, Mean Reversion;
- timeframe: `5m`, `15m`, `30m`, `1h`, `4h`;
- pair: BTC, ETH, SOL, LINK, UNI, AAVE, AVAX, TAO, XRP, ADA, DOGE;
- RSI, ATR, ADX, Relative Volume, Reward/Risk e regime di mercato.

Variabili Railway consigliate:

```env
RESEARCH_BATCH_SIZE=250
MAX_RESEARCH_RUNTIME_MINUTES=45
RESUME_RESEARCH=True
KRAKEN_API_SLEEP_SECONDS=1.2
KRAKEN_MAX_RETRIES=3
KRAKEN_TIMEOUT_SECONDS=20
```

La tabella `research_progress` salva avanzamento, batch corrente, totale combinazioni, combinazioni processate, ultimo strategy/pair/timeframe e stato. Se Railway interrompe il processo, al riavvio `RESUME_RESEARCH=True` riprende dal batch successivo.

### Output generati

Il motore salva tutto in `research_database.sqlite`, tabella `strategy_results`, senza scartare le strategie non validate. Genera inoltre:

- `top100_profit_factor.csv`;
- `top100_expectancy.csv`;
- `top100_winrate.csv`;
- `top100_sharpe.csv`;
- `top100_netprofit.csv`;
- `top100_lowest_drawdown.csv`;
- `research_summary.txt`.

Il report include TOP strategie, TOP pair, TOP timeframe, TOP strategy, TOP regimi, motivi di mancata validazione e suggerimenti automatici su parametri, pair, timeframe e strategie da approfondire. Il software non modifica automaticamente il bot live.

# Modular Quant Platform — Piano di rifattorizzazione

> Stato attuale: **FASE 1 completata**. Questa fase crea la nuova architettura modulare e implementa il solo **Data Collector**. Le fasi successive migreranno PostgreSQL/research/decision/strategy/notification senza riscrivere tutto in un unico passaggio.

## Nuova struttura cartelle

```text
project/
├── config/
│   └── settings.py
├── data_collector/
│   ├── kraken_client.py
│   ├── repository.py
│   └── service.py
├── database/
│   ├── migrations.py
│   ├── postgres.py
│   └── schema.py
├── decision_engine/
│   └── service.py
├── notification_engine/
│   └── service.py
├── research_engine/
│   └── service.py
├── shared/
│   ├── events.py
│   ├── indicators.py
│   ├── logging.py
│   ├── market_structure.py
│   ├── price_action.py
│   ├── risk.py
│   ├── utils.py
│   └── validators.py
├── strategy_engine/
│   └── service.py
├── logs/
├── scheduler.py
└── main.py
```

## Responsabilità dei moduli

| Modulo | Responsabilità | Stato |
| --- | --- | --- |
| `data_collector` | Scarica dati Kraken, aggiorna dati incrementali, evita duplicati, verifica integrità, scrive log sync. Non conosce strategie, indicatori o notifiche. | Fase 1 |
| `database` | Connessione PostgreSQL, schema `market_data`, `research`, `signals`, `statistics`, `system`, migrazioni idempotenti. | Fase 1 base |
| `shared` | Event bus, logging modulare, confini per indicatori, risk, price action, market structure e validator. | Fase 1 base |
| `research_engine` | Trova edge leggendo solo PostgreSQL e salvando risultati in `research.*`. | Fase 3 |
| `decision_engine` | Classifica regime e produce lista strategie abilitate. Non genera segnali. | Fase 4 |
| `strategy_engine` | Applica solo strategie validate/abilitate e produce eventi `NEW_SIGNAL`. | Fase 5 |
| `notification_engine` | Riceve eventi e invia Telegram/Discord/Email/Webhook senza decidere o calcolare. | Fase 6 |
| `scheduler` | Pianifica attività indipendenti per ogni modulo. | Fase 1 base |

## Dipendenze tra moduli

I moduli non devono chiamarsi direttamente per decisioni operative. La comunicazione passa dall'`EventBus` interno.

```text
Data Collector ──MARKET_UPDATED──▶ Event Bus ──▶ moduli subscriber futuri
Research Engine ─RESEARCH_COMPLETED/STRATEGY_VALIDATED──▶ Event Bus
Decision Engine ─MARKET_REGIME_CHANGED──▶ Event Bus
Strategy Engine ─NEW_SIGNAL/TRADE_OPENED/TRADE_CLOSED──▶ Event Bus
Notification Engine ◀── eventi dal Bus
```

## Diagramma architetturale

```text
                    ┌────────────────────┐
                    │  config/settings   │
                    └─────────┬──────────┘
                              │
┌──────────────┐      ┌────────▼────────┐      ┌──────────────────┐
│   Kraken     │─────▶│ Data Collector  │─────▶│ PostgreSQL       │
│ public OHLC  │      │ sync only       │      │ market_data.*    │
└──────────────┘      └────────┬────────┘      └────────┬─────────┘
                               │ MARKET_UPDATED          │
                               ▼                         │
                        ┌────────────┐                   │
                        │ Event Bus  │◀──────────────────┘
                        └─────┬──────┘
                              │
      ┌───────────────────────┼───────────────────────┐
      ▼                       ▼                       ▼
Research Engine          Decision Engine          Notification Engine
Phase 3                  Phase 4                  Phase 6
      │                       │                       ▲
      ▼                       ▼                       │
research.*              enabled strategies        events only
                              │
                              ▼
                       Strategy Engine
                       Phase 5 → NEW_SIGNAL
```

## Flusso dati della Fase 1

1. `project.main` carica le variabili ambiente tramite `project.config.settings`.
2. `database.migrations.run_migrations()` crea gli schema PostgreSQL idempotenti.
3. `DataCollectorService.sync_pair()` legge l'ultimo timestamp da `market_data.ohlc`.
4. `KrakenOhlcClient.fetch_ohlc()` scarica solo il range incrementale usando `since`.
5. `MarketDataRepository.insert_ohlc()` scrive in `market_data.ohlc` con `ON CONFLICT DO NOTHING`.
6. `market_data.sync_log` registra successo/fallimento, righe inserite ed eventuale errore.
7. Il servizio pubblica `MARKET_UPDATED` su `EventBus`.

## PostgreSQL Railway

La nuova piattaforma usa `DATABASE_URL` di Railway PostgreSQL:

```env
DATABASE_URL=${{Postgres.DATABASE_URL}}
ENABLE_DATA_COLLECTOR=true
ENABLE_RESEARCH_ENGINE=false
ENABLE_DECISION_ENGINE=false
ENABLE_STRATEGY_ENGINE=false
ENABLE_NOTIFICATION_ENGINE=false
COLLECTOR_PAIRS=BTC/USD,ETH/USD,SOL/USD
COLLECTOR_TIMEFRAMES=5m,15m,1h
KRAKEN_API_SLEEP_SECONDS=1.2
KRAKEN_MAX_RETRIES=3
KRAKEN_TIMEOUT_SECONDS=20
```

Il `Procfile` avvia la nuova piattaforma modulare:

```text
worker: python -m project.main
```

## Scheduler centrale

Intervalli target:

- Data Collector: ogni 5 minuti (`SCHEDULER_COLLECTOR_SECONDS=300`);
- Research Engine: ogni notte, da implementare in Fase 3;
- Decision Engine: ogni 15 minuti, da implementare in Fase 4;
- Strategy Engine: ogni minuto, da implementare in Fase 5;
- Notification Engine: real time via Event Bus, da implementare in Fase 6.

## Piano di migrazione

1. **Fase 1 — Architettura + Data Collector**: introdotta in questa modifica. Il vecchio codice resta disponibile, ma il nuovo entrypoint Railway è `project.main`.
2. **Fase 2 — PostgreSQL e migrazione dati**: migrare dati legacy SQLite verso PostgreSQL e consolidare repository per ogni schema.
3. **Fase 3 — Quant Research Engine**: spostare la ricerca in `project/research_engine`, leggere solo da PostgreSQL e scrivere `research.strategy_results`, `research.research_batches`, `research.research_reports`.
4. **Fase 4 — Decision Engine**: classificare `TREND_UP`, `TREND_DOWN`, `RANGE`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `COMPRESSION`, `BREAKOUT`, `NEWS_EVENT`, `NO_TRADE` e pubblicare strategie abilitate.
5. **Fase 5 — Strategy Engine**: generare segnali solo se strategia validata, abilitata, coerente con mercato e con reward/risk valido.
6. **Fase 6 — Notification Engine**: separare Telegram e predisporre Discord, Email e Webhook come subscriber di eventi.
