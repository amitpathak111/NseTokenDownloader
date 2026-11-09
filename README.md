# nse_tokens

Builds ONE shared token CSV from NSE's daily master files, so any project (tmcore, others) reads the same
data instead of each keeping its own copy.

## Setup
```
python -m venv .venv && .venv\Scripts\activate      # Linux/macOS: . .venv/bin/activate
pip install -r requirements.txt
```
Edit `config.yaml`: set `output_csv` to a location every project can read (absolute path recommended,
e.g. `C:/market_data/tokens.csv`).

## Daily use
```
python -m nse_tokens update          # download today's cash + F&O files, rebuild the CSV
python -m nse_tokens info            # how old is the CSV, how many rows per market code
```
Other commands: `download`, `import --fo F --cm C [--dry-run]` (for files you downloaded in a browser),
`fetch-server --url URL` (build from an internal token-server JSON instead).

NSE publishes the files after end of day (~6 PM IST) on trading days. On weekends/holidays or before
publication, `update` falls back to the previous trading day (`lookback_days`). Schedule it once a day
(Windows Task Scheduler); do not poll - NSE can block aggressive scripts. If NSE blocks the download,
fetch both files in a browser and use `import`.

## Previous close, volume, open interest (bhavcopy)
`update` also downloads NSE's daily bhavcopy and fills `Close`, `Volume` and (F&O) `OI` in the CSV:

| File | Used for | Joined by |
|---|---|---|
| `BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` | equity `Close` (ClsPric), `Volume` | exchange token (`FinInstrmId`) |
| `BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip` | F&O `Close`, `Volume`, `OI` (OpnIntrst) | exchange token |
| `ind_close_all_DDMMYYYY.csv` | index `Close` | index name |

- Same weekend/holiday fallback and "not today's file" warning as the master files.
- If a bhavcopy cannot be downloaded you get a WARNING and the tokens still build with the old values
  (`Close` 0 for equity). Check the log; `--no-bhav` or `bhav.enabled: false` skips it on purpose.
- `bhav.fo_close: settlement` uses the settlement price instead of the last close for F&O.
- Tokens with no bhav row (new listing, no trades) are counted as `bhav_unmatched_*` in the summary.
- Offline: `import --fo F --cm C --bhav-cm B1 --bhav-fo B2 --bhav-index B3`.
- `OI` is written exactly as NSE gives it (quantity, not lots). Check it against what tmcore expects.

## Output contract (what other projects can rely on)
`output_csv` columns, in order:
`ScriptCode, Symbol, ScriptName, Series, LotSize, Close, Volume, OI, Nifty, Future, Sector, Price_Jump,
Strike_Price, Option_Type, Expiry, MarketCode`
- `ScriptCode` is unique across the file; `Expiry` is ISO `YYYY-MM-DD`; prices are in rupees.
- `MarketCode` (0-14) is the segment bucket (see below). Consumers that derive their own may ignore it.
- Written atomically (temp file + rename) next to a sidecar `<csv>.meta.json` with `generated_at_utc`, row
  counts, source file names and column list - check it to detect a stale file.
- A failed run never overwrites a good CSV.

MarketCode: 0 FUTSTK, 1 index options (other), 2 OPTSTK, 3 equity (no F&O), 4 equity (with F&O),
5/6 index futures near/next month, 7/8/9/14 NIFTY/BANKNIFTY options by nearest expiry (1st-4th),
10 cash index, 11 FINNIFTY nearest, 12 MIDCPNIFTY nearest, 13 SME.

## Limits
- Written from NSE's published Masters Data spec; the downloaded `.csv.gz` layout was not verified against a
  real file. The importer checks every row and aborts (writing nothing) if the layout is not recognised -
  send the first 3 lines of the file if that happens.
- Previous-day Volume/OI and equity Close are not in the master files (written as 0).
- Index-option MarketCode comes from expiry order, an assumption - compare with your old data.
- Cash index rows get the ScriptCodes from `config.yaml` `indices` (not in NSE's security master).
- CM and F&O tokens share one file; if the same token number appears in both, the first is kept and the
  count is reported as `duplicate_token_dropped`.

## Tests
`python -m unittest discover -s tests -t .`
