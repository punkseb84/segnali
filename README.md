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


## Research Mode

Per impostazione predefinita il bot parte in `RESEARCH_MODE=true`. In questa modalità il worker **non invia segnali operativi live**: scarica dati OHLC Kraken, testa strategie storiche, separa i risultati in 70% training e 30% validation e genera un report di ricerca.

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
RESEARCH_MODE=true
RESEARCH_OHLC_LIMIT=720
RESEARCH_MIN_TRADES=200
RESEARCH_TIMEFRAMES=5m,15m,1h
RESEARCH_INTERVAL_SECONDS=86400
```

Per riattivare i segnali live in futuro, imposta `RESEARCH_MODE=false`, ma solo dopo aver validato una strategia con edge statistico.

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
RESEARCH_MODE=true
RESEARCH_OHLC_LIMIT=720
RESEARCH_MIN_TRADES=200
RESEARCH_TIMEFRAMES=5m,15m,1h
RESEARCH_INTERVAL_SECONDS=86400
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
