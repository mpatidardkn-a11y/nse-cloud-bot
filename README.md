# NSE report bot (GitHub Actions)

Runs on GitHub's servers, so your computer can be off.

| When (IST, Mon-Fri, NSE trading days) | What |
|---|---|
| 8:00 | Pre-market analysis: indices, sectors, pivots, news, top trade setups. Email with PDF + Excel |
| 9:35, 9:50 ... 15:20 | Top 10 stocks from that morning's setups: Entry, Stop Loss, Target 1, Target 2. Email with PDF |

Secrets needed (Settings -> Secrets and variables -> Actions): `EMAIL_FROM`, `EMAIL_APP_PASSWORD`, `EMAIL_TO`.

On-time delivery: `day.yml` starts at 06:30 IST (backup 07:15) and the bot itself waits for 07:58 and each 15-minute slot,
so GitHub's late scheduling doesn't delay emails (each report lands ~3 min after its slot). Needs a public repo (free unlimited Actions minutes).
`morning.yml` and `intraday.yml` are manual tests only.
NSE holidays for 2026 are built in; for 2027 add `trader/nse_holidays.json`
(`{"years": [2027], "holidays": {"2027-01-26": "Republic Day", ...}}`).

Only stocks with market capitalisation above Rs 20,000 Cr are analysed (set `MIN_MCAP_CR` to change).

Educational technical scan on free, delayed data. Not investment advice.
