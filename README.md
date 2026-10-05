# Trade Copier

Copies trades from one MetaTrader 5 **master** account to any number of MetaTrader 5 **slave** accounts. You trade by hand on the master, and the copier places the matching trades on every slave, sized for that account.

What it copies:

- **New trades:** market positions and pending orders (buy/sell limit and stop).
- **Stop loss and take profit:** any change you make on the master is copied over.
- **Pending order price changes.**
- **Partial closes:** close 40% on the master and 40% closes on each slave.
- **Full closes and deleted pending orders.**

Each slave has its own settings:

- **Lot sizing.** One of:
  - a fixed multiplier (10x for a 10k master and a 100k slave)
  - automatic sizing by balance or equity ratio, with currency conversion (GBP master to USD slave)
  - a fixed lot
  - percent risk per trade, based on the master's stop loss
- **Limits:** max lot, min lot, max open positions, and a drawdown cut-off that stops new trades.
- **Symbol names:** maps names between brokers (`XAUUSD` to `GOLD`, `EURUSD` to `EURUSD.pro`) and evens out different contract sizes.
- **Filters:** only these symbols, never these symbols, buys only or sells only.
- **Reverse mode,** which takes the opposite trade.
- **Dry run,** which logs everything and sends nothing.

It works from complete snapshots of the master rather than one-off events. So if the copier, a terminal or the internet goes down, the next snapshot puts every slave back in line: trades you closed while it was down get closed, and nothing gets opened twice.

---

## How it works

```
 MT5 terminal (master) ──► master process ──snapshot──┬──► slave process ──► MT5 terminal (slave 1)
                                                      ├──► slave process ──► MT5 terminal (slave 2)
                                                      └──► ...
```

The official `MetaTrader5` Python package can only talk to **one terminal per process**. Because of that:

- every account needs **its own MetaTrader 5 installation folder**
- the copier runs one process per account and starts them all for you

Every trade the copier opens on a slave gets two tags:

- a **magic number** (one per slave, set in the config)
- the comment `TC|<master ticket>`

The copier only ever touches trades carrying its own magic number, so trades you place by hand on a slave are left alone. The comment lets it rebuild its records if its state file is lost.

---

## Setup on Windows

### 1. Install Python

Install **Python 3.11 (64-bit)** from [python.org](https://www.python.org/downloads/windows/), and tick **"Add Python to PATH"** during install. The `MetaTrader5` package needs 64-bit Python on Windows. 3.11 and 3.12 are the safe choices.

Install [Git for Windows](https://git-scm.com/download/win) too, if you don't have it.

### 2. Install one MetaTrader 5 terminal per account

Every account (the master and each slave) needs a separate MT5 install. Two ways to get one:

- **Option A: use the installer several times.** Run your broker's MT5 installer. Click **Settings** on the first screen and pick a different folder each time, for example:
  - `C:\MT5\Master`
  - `C:\MT5\Slave1`
  - `C:\MT5\Slave2`
- **Option B: copy an existing install.** Copy an installed MT5 folder, for example `C:\Program Files\MetaTrader 5`, to `C:\MT5\Slave1`, and set `portable: true` for that account in the config.

Then, in **each** terminal:

1. Log in to its account.
2. Go to **Tools → Options → Expert Advisors**, tick **Allow algorithmic trading**, and untick the options that disable algo trading when the account or profile changes.
3. Make sure the **Algo Trading** button on the toolbar is green. Slave orders are rejected while it is off.

Slave accounts should ideally be **hedging** accounts. On a netting account, trades on the same symbol merge into one position.

### 3. Download the copier

Open **PowerShell** and run:

```powershell
cd C:\
git clone https://github.com/WilsonsPage/Trade-Copier.git
cd Trade-Copier
.\setup.bat
```

`setup.bat` does three things:

- creates a virtual environment in `.venv`
- installs the requirements (`MetaTrader5`, `PyYAML`)
- copies `config.example.yaml` to `config.yaml`

### 4. Configure

Open `config.yaml` in any text editor. The example file explains every setting. At minimum, set:

- each account's `terminal_path` (for example `C:/MT5/Slave1/terminal64.exe`)
- each account's `login` and `server`
- the lot sizing for each slave

**Passwords.** The safest way is to keep them out of the file. Write `password: ${MT5_SLAVE1_PASSWORD}` in the config, then set the variable once in PowerShell:

```powershell
setx MT5_MASTER_PASSWORD "your-master-password"
setx MT5_SLAVE1_PASSWORD "your-slave-password"
```

Close and reopen PowerShell afterwards so the new variables are picked up. You can also leave `login`, `password` and `server` out completely if each terminal is already logged in and remembers its password.

`config.yaml` is listed in `.gitignore`, so your account details are never pushed to GitHub.

### 5. Check everything, then run

```powershell
.venv\Scripts\python.exe -m trade_copier validate   # checks the config file
.venv\Scripts\python.exe -m trade_copier check      # connects to every terminal and reports
```

`check` shows:

- each account's balance, currency and account type
- whether Algo Trading is on in each slave terminal
- the exchange rate it will use between account currencies
- whether every mapped symbol exists at the slave broker

Fix anything it flags, then start copying:

```powershell
.venv\Scripts\python.exe -m trade_copier run
```

You can also just double-click `start_copier.bat`. Press **Ctrl+C** to stop.

**Try it on demo accounts first**, or with `dry_run: true` on a slave. In dry run the copier logs every trade it would make and sends nothing.

Logs go to `logs\<account name>.log`. The copier's records of which master trade maps to which slave trade live in `state\`.

### Running it 24/7

Run it on a Windows VPS, or a PC that stays on. To start it automatically, put a shortcut to `start_copier.bat` in the Startup folder: press Win+R, type `shell:startup`, and paste the shortcut there. The MT5 terminals must stay running and logged in. The copier starts them if they're closed.

---

## Lot sizing examples

| You want | Config |
|---|---|
| 10k master to 100k slave at 10x | `mode: multiplier`, `multiplier: 10` |
| Size automatically by account size, in any currency | `mode: balance_ratio` (a 10k GBP master and a 127k USD slave gives 10x) |
| Same, but half the risk | `mode: balance_ratio`, `multiplier: 0.5` |
| Always 0.5 lots | `mode: fixed`, `fixed_lot: 0.5` |
| Risk 1% of the slave's equity per trade | `mode: risk_percent`, `risk_percent: 1` (needs a stop loss on the master trade) |

After the mode is applied, the copier:

1. applies `max_lot` and `min_lot`
2. rounds down to the broker's lot step
3. skips the trade if the result is below the broker's minimum lot, unless you set `round_up_to_min_lot: true`

`normalize_contract_size` (on by default) adjusts for brokers with different contract sizes. For example, gold might be 100 oz per lot at one broker and 10 oz at another.

## Behaviour worth knowing

- **First start:** trades already open on the master are **not** copied unless `copy_existing_on_start: true`. Trades opened while the copier was stopped **are** copied on the next start. Set `max_price_deviation_points` if you don't want a late entry at a much worse price.
- **Closed on the slave:** if you close a copied trade on a slave yourself, or its own SL/TP is hit, it is not reopened.
- **Master unreachable:** if the master terminal can't be read, the slaves hold their trades as they are. A lost connection never closes anything.
- **Pending orders:** if the master's pending order fills but the slave's hasn't, the slave's order is replaced with a market order. Set `pending_fill_to_market: false` to leave it pending instead.
- **Stops too close:** if a slave broker rejects the SL/TP as too close, the trade is opened without them, and the copier keeps trying to set them.
- **Adding to a position:** adding to an existing position on a netting master isn't copied (only reductions are). New positions are.

## Development

The trading logic is separated from MetaTrader, so the tests run on any OS with a simulated terminal:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

| File | What's in it |
|---|---|
| `trade_copier/copier.py` | reconciliation: open, modify, partial close, close |
| `trade_copier/sizing.py` | lot size calculation |
| `trade_copier/symbols.py`, `fx.py` | symbol mapping and currency conversion |
| `trade_copier/broker.py` | the only file that talks to the `MetaTrader5` package |
| `trade_copier/runner.py` | the one-process-per-terminal orchestration |

## Disclaimer

Trading carries risk. This software places real orders on your accounts. Test it on demo accounts first, and use it at your own risk.
