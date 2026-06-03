# 🎰 Optimus Casino Bot

A feature-rich Telegram Casino Bot with multiple games, crypto payments, referral system, and admin management.

---

## 🚀 Quick Deploy

### Option 1: Docker (Recommended)
```bash
git clone https://github.com/deco94816-design/optimus-.git
cd optimus-
cp .env.example .env
# Edit .env with your bot token
docker-compose up -d
```

### Option 2: Python (Manual)
```bash
git clone https://github.com/deco94816-design/optimus-.git
cd optimus-
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
python "casino v5 (1).py"
```

### Option 3: Windows One-Click
```
Double-click run_bot.bat
```

---

## 🎮 Games

| Game | Type | Multiplier |
|------|------|-----------|
| 🎲 Dice Battle | Predict roll (1-6) | 1.92x |
| 🎯 Darts | Predict score (1-6) | 1.92x |
| ⚽ Football | Predict outcome (1-5) | 1.92x |
| 🏀 Basketball | Predict shot (1-5) | 1.92x |
| 🎳 Bowling | Predict pins (0-6) | 1.92x |
| 💣 Mines | Multi-level mine game | Variable |
| 🪙 Coinflip | Heads or Tails | 1.92x |
| 🃏 Blackjack | Classic card game | 2x |
| 🔮 Predict | Multi-option prediction | Variable |

---

## 💰 Features

- **Telegram Stars** — In-app currency with deposits & withdrawals
- **Crypto Payments** — USDT, BTC, ETH, LTC, DOGE via OxaPay
- **Leveling System** — 26 levels with rakeback rewards
- **Referral Program** — Code-based friend referrals with bonuses
- **Weekly Bonuses** — Auto-generated weekly rewards
- **Game History** — Complete play records per user
- **Support Tickets** — Built-in issue reporting
- **Multi-language** — Extensible language support
- **Auto-backups** — Database backups on startup

---

## 🛠️ Admin Commands

```
/ban <user_id> <reason>     — Ban user globally
/unban <user_id>            — Remove ban
/wd <amount>                — Set minimum withdrawal
/set <coin> <address>       — Set crypto deposit address
/stats                      — View bot statistics
/gift <user_id> <amount>    — Send Stars gift
```

---

## 📁 Project Structure

```
optimus-/
├── casino v5 (1).py      # Main bot application
├── storage.py             # Database layer (SQLite)
├── bot_network.py         # Multi-bot registry & blacklist
├── languages.py           # i18n support
├── oxapay.py              # Crypto payment integration
├── streaming_funcs.py     # Streaming utilities
├── launch_bot.py          # Pre-launch verification script
├── run_bot.bat            # Windows one-click launcher
├── requirements.txt       # Python dependencies
├── Dockerfile             # Docker container build
├── docker-compose.yml     # Docker Compose deployment
└── .env.example           # Environment variable template
```

---

## ⚙️ Configuration

1. Get a bot token from [@BotFather](https://t.me/BotFather)
2. Copy `.env.example` to `.env`
3. Set your `BOT_TOKEN` in `.env`
4. Update admin IDs in the bot code if needed

---

## 📋 Requirements

- Python 3.10+
- `python-telegram-bot` >= 21.0
- `httpx` >= 0.27.0
- `Pillow` >= 10.0.0

---

## 📄 License

Private — All rights reserved.
