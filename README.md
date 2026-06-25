# Kraken Crypto Signal Bot

Worker Python pronto per Railway che monitora coppie crypto su Kraken, calcola indicatori tecnici e invia segnali automatici LONG/SHORT su Telegram.

> Questo progetto usa solo dati pubblici OHLCV di Kraken tramite `ccxt`: non servono API key dell'exchange. Non usa Binance.

## Funzionalità

- Worker continuo, senza web server.
- Scan ogni 5 minuti.
- Exchange: Kraken.
- Timeframe principale: 15m.
- Conferma trend: 1h.
- Indicatori:
  - EMA 20
  - EMA 50
  - EMA 200
  - RSI 14
  - MACD 12/26/9
  - ATR 14
  - Volume medio 20 periodi
- Risk management automatico:
  - Entry sull'ultima chiusura.
  - Stop Loss basato su 1.5 ATR.
  - Target 1 e Target 2 basati sul rischio.
- Invio alert Telegram.
- Messaggio Telegram di avvio.
- Anti-duplicazione dei segnali tramite file JSON locale.
- Logging leggibile per Railway.

## Coppie monitorate

Le coppie predefinite sono definite in `main.py`:

```python
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "BNB/USD", "DOGE/USD"]
```

Se Kraken non rende disponibile una coppia, il bot la salta e scrive un warning nei log.

## Strategia

### Condizioni LONG

1. Su timeframe 1h il prezzo è sopra EMA 200.
2. Su timeframe 15m EMA 20 > EMA 50 > EMA 200.
3. RSI 15m compreso tra 50 e 70.
4. MACD histogram passa da negativo a positivo.
5. Volume ultima candela > media volume ultime 20 candele.

### Condizioni SHORT

1. Su timeframe 1h il prezzo è sotto EMA 200.
2. Su timeframe 15m EMA 20 < EMA 50 < EMA 200.
3. RSI 15m compreso tra 30 e 50.
4. MACD histogram passa da positivo a negativo.
5. Volume ultima candela > media volume ultime 20 candele.

## File del progetto

- `main.py` - worker principale.
- `requirements.txt` - dipendenze Python.
- `Procfile` - comando worker Railway.
- `.env.example` - esempio variabili ambiente.
- `README.md` - documentazione.

## Esecuzione locale

1. Crea un ambiente virtuale:

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Installa le dipendenze:

```bash
pip install -r requirements.txt
```

3. Crea il file `.env` partendo dall'esempio:

```bash
cp .env.example .env
```

4. Inserisci nel file `.env` i tuoi valori reali:

```env
TELEGRAM_BOT_TOKEN=123456789:token_reale
TELEGRAM_CHAT_ID=123456789
```

5. Avvia il worker:

```bash
python main.py
```

## Variabili ambiente

| Variabile | Obbligatoria | Descrizione |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Sì | Token del bot Telegram creato con BotFather. |
| `TELEGRAM_CHAT_ID` | Sì | ID della chat, gruppo o canale dove inviare i segnali. |
| `LOG_LEVEL` | No | Livello log, default `INFO`. |
| `SIGNAL_STATE_FILE` | No | Percorso file JSON anti-duplicazione, default `last_signals.json`. |

## Deploy su Railway

Railway usa il `Procfile` incluso:

```Procfile
worker: python main.py
```

Non devi esporre porte HTTP perché questa app è un worker continuo.

## Configurazione passo per passo

### 1. Come creare un bot Telegram con BotFather

1. Apri Telegram.
2. Cerca `@BotFather`.
3. Avvia la chat con `/start`.
4. Invia il comando `/newbot`.
5. Scegli un nome visibile per il bot, ad esempio `Crypto Signal Bot`.
6. Scegli uno username che termini con `bot`, ad esempio `mio_crypto_signal_bot`.

### 2. Come ottenere TELEGRAM_BOT_TOKEN

Dopo la creazione del bot, BotFather ti invia un token simile a:

```text
123456789:ABCDEF_token_di_esempio
```

Questo valore va inserito nella variabile ambiente:

```env
TELEGRAM_BOT_TOKEN=123456789:ABCDEF_token_di_esempio
```

Non pubblicare mai il token nel repository GitHub.

### 3. Come ottenere TELEGRAM_CHAT_ID

Metodo semplice per una chat privata:

1. Apri Telegram e scrivi un messaggio al bot appena creato, ad esempio `ciao`.
2. Apri nel browser questo URL, sostituendo il token:

```text
https://api.telegram.org/botTELEGRAM_BOT_TOKEN/getUpdates
```

3. Cerca nel JSON il campo `chat` e poi `id`.
4. Usa quel valore come `TELEGRAM_CHAT_ID`.

Per gruppi o canali:

1. Aggiungi il bot al gruppo o al canale.
2. Invia un messaggio nel gruppo o canale.
3. Usa lo stesso endpoint `getUpdates`.
4. Copia l'ID della chat. Nei gruppi spesso è un numero negativo.

### 4. Come creare il repository GitHub

1. Accedi a GitHub.
2. Clicca su **New repository**.
3. Dai un nome al repository, ad esempio `kraken-crypto-signal-bot`.
4. Scegli se renderlo pubblico o privato.
5. Non inserire token o file `.env` nel repository.
6. Crea il repository.

### 5. Quali file caricare nel repository

Carica questi file:

- `main.py`
- `requirements.txt`
- `Procfile`
- `README.md`
- `.env.example`

Non caricare:

- `.env`
- `last_signals.json`
- cartelle virtualenv come `.venv/`
- file temporanei o cache Python

### 6. Come collegare GitHub a Railway

1. Accedi a Railway.
2. Vai nella dashboard.
3. Collega il tuo account GitHub dalle impostazioni o durante la creazione del progetto.
4. Autorizza Railway a leggere il repository del bot.

### 7. Come creare un nuovo progetto Railway

1. Clicca su **New Project**.
2. Seleziona **Deploy from GitHub repo**.
3. Scegli il repository del bot.
4. Railway rileverà il progetto Python e installerà le dipendenze da `requirements.txt`.

### 8. Come impostare le variabili ambiente su Railway

1. Apri il progetto Railway.
2. Seleziona il servizio del bot.
3. Vai su **Variables**.
4. Aggiungi:

```env
TELEGRAM_BOT_TOKEN=il_token_del_tuo_bot
TELEGRAM_CHAT_ID=il_tuo_chat_id
```

Opzionale:

```env
LOG_LEVEL=INFO
```

### 9. Come avviare il worker su Railway

Il file `Procfile` contiene già:

```Procfile
worker: python main.py
```

Dopo il deploy, Railway avvia il processo come worker. Se necessario, controlla nelle impostazioni del servizio che il comando di start sia `python main.py` o che Railway stia usando il `Procfile`.

### 10. Come verificare dai log che il bot stia funzionando

Apri la sezione **Logs** del servizio Railway e cerca messaggi simili a:

```text
Starting Kraken crypto signal worker
Starting scan cycle
Checking BTC/USD
No signal for BTC/USD
Scan cycle finished
```

All'avvio dovresti anche ricevere su Telegram:

```text
🤖 Crypto signal bot avviato. Monitoraggio Kraken attivo.
```

### 11. Come modificare in futuro le coppie da monitorare

Apri `main.py` e modifica la lista:

```python
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "BNB/USD", "DOGE/USD"]
```

Esempio:

```python
SYMBOLS = ["BTC/USD", "ETH/USD", "ADA/USD"]
```

Poi fai commit, push su GitHub e Railway redeployerà il worker.

### 12. Come modificare il messaggio Telegram

Apri `main.py` e modifica la funzione:

```python
def format_signal_message(signal):
```

Puoi cambiare testi, emoji, ordine dei campi o aggiungere nuovi dettagli calcolati dalla strategia.

### 13. Come fare troubleshooting se Telegram non riceve messaggi

Controlla questi punti:

1. `TELEGRAM_BOT_TOKEN` è corretto e senza spazi.
2. `TELEGRAM_CHAT_ID` è corretto.
3. Hai scritto almeno un messaggio al bot prima di usare `getUpdates`.
4. Se usi un gruppo, il bot è stato aggiunto al gruppo.
5. Nei log Railway non ci sono errori `Telegram send failed`.
6. Il token non è stato rigenerato da BotFather.
7. Il bot non è stato bloccato dalla chat destinataria.

Per testare manualmente, apri nel browser:

```text
https://api.telegram.org/botTELEGRAM_BOT_TOKEN/sendMessage?chat_id=TELEGRAM_CHAT_ID&text=test
```

sostituendo i valori reali.

### 14. Come fare troubleshooting se Kraken non restituisce dati

Controlla questi punti:

1. Verifica nei log eventuali warning su simboli non disponibili.
2. Alcune coppie possono avere simboli diversi o liquidità diversa su Kraken.
3. Controlla che Railway abbia accesso a Internet.
4. Verifica che Kraken non stia applicando rate limit temporanei.
5. Riprova dopo qualche minuto se vedi errori di rete o rate limit.
6. Se vuoi rimuovere una coppia problematica, modifica `SYMBOLS` in `main.py`.

### 15. Come aggiornare il codice e redeployare su Railway

1. Modifica i file localmente.
2. Testa con:

```bash
python main.py
```

3. Esegui commit:

```bash
git add .
git commit -m "Update crypto signal bot"
```

4. Fai push su GitHub:

```bash
git push
```

5. Railway rileverà il push e avvierà un nuovo deploy.
6. Controlla i log Railway per verificare che il worker sia partito correttamente.

## Avvertenza

Questo bot genera segnali tecnici automatici e non costituisce consulenza finanziaria. Testa sempre la strategia prima di usarla con capitale reale.
