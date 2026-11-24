# NSE report bot (GitHub Actions)

Runs on GitHub's servers, so your computer can be off.

| When (IST, Mon-Fri, NSE trading days) | What |
|---|---|
| ~8:00 | Pre-market analysis: indices, sectors, pivots, news, top trade setups. Email with PDF + Excel |
| 9:35, 9:50 ... 15:20 | Top 10 stocks from that morning's setups: Entry, Stop Loss, Target 1, Target 2. Email with PDF |

Secrets needed (Settings -> Secrets and variables -> Actions): `EMAIL_FROM`, `EMAIL_APP_PASSWORD`, `EMAIL_TO`.

GitHub can start scheduled runs a few minutes late at busy times.
NSE holidays for 2026 are built in; for 2027 add `trader/nse_holidays.json`
(`{"years": [2027], "holidays": {"2027-01-26": "Republic Day", ...}}`).

Educational technical scan on free, delayed data. Not investment advice.
