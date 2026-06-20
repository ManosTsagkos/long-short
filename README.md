# BTC Composite Signal Bot για WunderTrading

## Πρώτα, μια ειλικρινής διευκρίνιση

Δεν έχω γράψει τον κώδικα του budsignal.io και δεν έχω καμία σχέση μαζί του.
Διάβασα τη σελίδα τους (https://budsignal.io/) και αυτό που βλέπει κανείς εκεί
είναι **marketing copy**, όχι τεχνική τεκμηρίωση. Η σελίδα λέει ότι
χρησιμοποιούν τρεις κατηγορίες δεδομένων:

- **"Pressure"** → order flow: open interest, "whale vs retail delta", bid/ask depth
- **"Momentum"** → EMA, MACD, RSI
- **"Liquidity"** → liquidation heatmap (αναφέρουν το Hyblock Capital)

…και ότι τα σήματα LONG/SHORT βγαίνουν μόνο όταν αυτά τα "independent reads"
συμφωνούν ("aligned bullish/bearish"), με κάποιο μηχανισμό
**reconfirmation** ("Reconfirmed 1×").

Αυτό είναι όλο όσο αποκαλύπτουν. Δεν δημοσιεύουν τους ακριβείς συντελεστές
βαρύτητας, τα κατώφλια (thresholds), τα lookback periods, ή τον ακριβή
αλγόριθμο τους — αυτό είναι το επί πληρωμή προϊόν τους ($99-149/μήνα). Άρα
δεν υπάρχει κάτι συγκεκριμένο να "αντιγραφεί" 1:1, γιατί δεν είναι δημόσιο.

Αυτό που σου έφτιαξα παρακάτω είναι ένα **δικό μου, από το μηδέν, ανάλογο
σύστημα της ίδιας κατηγορίας**: ένα multi-factor composite που χρειάζεται
συμφωνία ανάμεσα σε ανεξάρτητους δείκτες πριν βγάλει σήμα, χτισμένο πάνω σε
δημόσια δεδομένα futures του Binance, με πραγματική, τεκμηριωμένη σύνδεση
στο WunderTrading.

**Δεν είναι οικονομική συμβουλή.** Δεν μπορώ να σου εγγυηθώ ποσοστό επιτυχίας
85% ή οποιοδήποτε ποσοστό — αυτό είναι κάτι που λέει η σελίδα τους για το
δικό τους, άγνωστο σε εμένα, σύστημα. Κάνε backtesting / paper trading πριν
βάλεις πραγματικό κεφάλαιο.

---

## Η τακτική (η δομή του συστήματος)

Το bot δουλεύει σε **BTCUSDT perpetual futures, candle 4H** (swing, όχι
scalping) και παράγει μία από τρεις καταστάσεις: **LONG / SHORT / WAIT**.

Υπολογίζει 3 "πυλώνες", ο καθένας δίνει ψήφο -1 (bearish) / 0 (ουδέτερο) / +1
(bullish):

### 1. Momentum (τεχνικό)
- EMA21 έναντι EMA55 (τάση)
- MACD histogram (πρόσημο)
- RSI(14) έναντι 50 (ζώνη ουδετερότητας 45-55)
- Χρειάζονται **2 από τα 3** να συμφωνούν για να βγει ψήφος, αλλιώς 0.

### 2. Pressure (order flow)
- Open Interest σε σχέση με την τιμή (αύξηση OI + άνοδος τιμής = bullish
  continuation· αύξηση OI + πτώση τιμής = bearish continuation· πτώση OI =
  αδιάφορο)
- Taker buy/sell ratio (επιθετικότητα αγοραστών/πωλητών)
- **Whale vs retail delta**: σύγκριση του long/short ratio των μεγάλων
  traders (`topLongShortPositionRatio`) με αυτό όλων των λογαριασμών
  (`globalLongShortAccountRatio`) — όταν διαφέρουν σημαντικά, δείχνει ποια
  πλευρά είναι διαφορετικά τοποθετημένη από το retail
- Bid/ask depth imbalance κοντά στην τιμή
- Χρειάζεται **≥60% συμφωνία** μεταξύ των μη-ουδέτερων ψήφων

### 3. Liquidity (ρευστοποιήσεις)
- Funding rate ακραίο (πολύ θετικό = πολλοί μοχλευμένοι long → ρίσκο
  ρευστοποίησης προς τα κάτω· πολύ αρνητικό = το αντίστροφο)
- Απόσταση τιμής από recent swing high/low (εκεί συγκεντρώνονται stops και
  ρευστοποιήσεις — proxy για το πραγματικό liquidation heatmap, αφού τα
  πραγματικά δεδομένα του Hyblock Capital είναι επί πληρωμή API)

### Composite κανόνας
- Χρειάζονται **τουλάχιστον 2 από τους 3 πυλώνες** να συμφωνούν στην ίδια
  κατεύθυνση.
- Αυτή η συμφωνία πρέπει να **επιβεβαιωθεί (reconfirm)** για
  `RECONFIRM_BARS` συνεχόμενα κλεισμένα candles πριν στείλει σήμα — ακριβώς
  η λογική του "Reconfirmed" badge που βλέπεις στη σελίδα τους.
- Διαφορετικά → WAIT, δεν στέλνεται τίποτα.

### Αν θέλεις να ανέβει το επίπεδο
Αν αποκτήσεις συνδρομή στο **Hyblock Capital API** (το ίδιο που αναφέρει το
budsignal.io), μπορείς να αντικαταστήσεις το proxy της `liquidity_vote()`
(funding rate + swing high/low) με το πραγματικό τους liquidation heatmap
endpoint. Η δομή του bot (πυλώνες → ψήφοι → composite → reconfirmation →
webhook) παραμένει ίδια.

---

## Αρχεία

- `bud_style_btc_signal_bot.py` — το κύριο bot (Python 3.9+)
- `requirements.txt` — εξαρτήσεις

## Πώς λειτουργεί τεχνικά

1. Κάθε `POLL_SECONDS` δευτερόλεπτα, τραβάει τα τελευταία 4H candles από το
   public API του Binance Futures (δεν χρειάζεται API key για market data).
2. Όταν ανιχνεύσει **νέο κλεισμένο candle**, υπολογίζει τους 3 πυλώνες και
   το composite σήμα.
3. Κρατάει state σε ένα τοπικό αρχείο JSON (`bud_bot_state.json`) ώστε να
   ξέρει αν το σήμα είναι ήδη "ανοιχτό" και πόσα candles έχει επιβεβαιωθεί.
4. Όταν ένα νέο, επιβεβαιωμένο σήμα διαφέρει από την τρέχουσα θέση, στέλνει
   `POST` request με JSON στο webhook URL του WunderTrading bot σου.

## Ρύθμιση στο WunderTrading

1. **Signal Bot → Create bot**
2. General tab: επίλεξε exchange/API, BTCUSDT pair, ενεργοποίησε
   **Swing trade** (ON) αν θες auto-flip σε Futures.
3. Entries tab:
   - **Bot start condition** → `API request` (όχι TradingView, αφού το
     σήμα έρχεται από τον δικό σου server)
   - **Bot settings format** → `JSON`
4. Πήγαινε στο **Settings → Bot Server IP** και πρόσθεσε το IP του server
   όπου θα τρέχει το bot (αλλιώς το WunderTrading θα αγνοεί τα requests).
5. Στο **Alerts tab** θα δεις:
   - το **Webhook URL** (κανονικά: `https://wtalerts.com/bot/custom`)
   - τους κωδικούς **Enter-Long / Enter-Short / Exit-All** (default
     ονόματα, μπορείς να τα αλλάξεις) — αυτά είναι τα `code` που μπαίνουν
     στο JSON payload.

## Ρύθμιση του bot

```bash
pip install -r requirements.txt

export WUNDER_WEBHOOK_URL="https://wtalerts.com/bot/custom"
export WUNDER_LONG_CODE="το-δικό-σου-enter-long-code"
export WUNDER_SHORT_CODE="το-δικό-σου-enter-short-code"
export WUNDER_EXIT_CODE="το-δικό-σου-exit-all-code"

export BUD_SYMBOL="BTCUSDT"
export BUD_INTERVAL="4h"
export BUD_RECONFIRM_BARS="1"
export BUD_LEVERAGE="2"
export BUD_AMOUNT_PER_TRADE="0.1"      # 10% του capital ανά trade
export BUD_STOP_LOSS_PCT="0.015"       # 1.5%
export BUD_TAKE_PROFIT_PCT="0.03"      # 3%

export BUD_DRY_RUN="true"   # ΚΡΑΤΑ ΤΟ true μέχρι να το δεις να δουλεύει σωστά στα logs

python3 bud_style_btc_signal_bot.py
```

Όσο `BUD_DRY_RUN=true`, το bot υπολογίζει τα σήματα και τα τυπώνει στα logs
**χωρίς** να στείλει πραγματικό request στο WunderTrading. Άλλαξέ το σε
`false` μόνο αφού δεις αρκετούς κύκλους να βγάζουν λογικά αποτελέσματα.

Πρακτικά, καλό είναι να το τρέχεις σαν service (π.χ. `systemd`, `pm2`, ή
απλά μέσα σε `screen`/`tmux` σε ένα VPS) ώστε να μένει ενεργό 24/7, αφού
δουλεύει σε 4H candles.

## Παράμετροι του JSON που στέλνεται (σχήμα WunderTrading)

```json
{
  "code": "Enter-Long",
  "orderType": "market",
  "amountPerTradeType": "percents",
  "amountPerTrade": 0.1,
  "leverage": 2,
  "stopLoss": { "priceDeviation": 0.015 },
  "takeProfits": [{ "priceDeviation": 0.03, "portfolio": 1 }],
  "reduceOnly": false
}
```

Αυτά τα πεδία (`code`, `orderType`, `amountPerTradeType`, `amountPerTrade`,
`leverage`, `stopLoss`, `takeProfits`, `reduceOnly`) είναι ακριβώς τα πεδία
που δέχεται το WunderTrading Signal Bot σε JSON mode — τα πήρα από την
επίσημη τεκμηρίωσή τους, όχι από το budsignal.io.

## Περιορισμοί / πράγματα που πρέπει να ξέρεις

- Το `liquidity_vote()` είναι ένα **proxy**, όχι πραγματικό liquidation
  heatmap. Funding rate + θέση μέσα σε recent range είναι μια λογική, αλλά
  ατελής, εκτίμηση του πού συγκεντρώνονται ρευστοποιήσεις.
- Δεν υπάρχει καμία εγγύηση win rate. Ο,τιδήποτε ποσοστό βλέπεις σε σελίδες
  σαν το budsignal.io αναφέρεται σε *δικό τους* ιστορικό, με *δικά τους*
  (άγνωστα σε όλους εκτός τους ίδιους) κατώφλια.
- Backtest πρώτα σε ιστορικά δεδομένα, μετά paper trade, μετά μικρό
  κεφάλαιο, πριν τρέξεις κανονικό μέγεθος θέσης.
- Trading με leverage σε futures έχει ρίσκο ολικής απώλειας κεφαλαίου.
