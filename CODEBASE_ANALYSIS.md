# Telegram Casino Bot - Codebase Analysis

## 📋 Overview
This is a **multi-bot Telegram casino platform** with support for games, crypto payments, deposits/withdrawals, multi-language support, and an admin dashboard. The system uses SQLite for persistence and supports cross-bot user management with shared blacklisting.

**Date Analyzed:** May 23, 2026  
**Technology Stack:** Python, python-telegram-bot, SQLite, OxaPay API, PIL (Image processing)

---

## 📁 Project Structure

```
Telegram Desktop/
├── bot_network.py          # Multi-bot registry + shared blacklist
├── casino v5 (1).py        # Main bot logic (games, deposits, admin)
├── storage.py              # SQLite persistence layer
├── languages.py            # Multi-language support
├── oxapay.py               # Crypto payment integration
└── (Generated DBs)
    ├── bot_network.db      # Cross-bot registry & blacklist
    ├── casino_data.db      # User data, balances, history
    ├── templates.db        # Message templates (reference only)
    └── emoji_mappings.db   # Custom emoji mappings
```

---

## 🔧 Module Breakdown

### 1. **storage.py** - SQLite Persistence Layer
**Purpose:** Thread-safe database abstraction for casino data  
**Key Components:**

#### Database Schema
| Table | Purpose |
|-------|---------|
| `settings` | Global config (bankroll, min withdrawal, language, etc.) |
| `users` | User balances, bans, language preferences |
| `profiles` | Player stats (XP, games, wins, bets, registration date) |
| `game_history` | Bet/win records per user |
| `admins` | Admin user IDs |
| `referral_codes` | User referral code mapping |
| `referrers` | Referred user tracking |
| `referral_stats` | Lifetime earnings & withdrawable balance |
| `tickets` | Support tickets |
| `withdrawals` | Withdrawal TX records (Stars/TON) |
| `deposits` | Crypto deposit tracking |
| `username_map` | Username → user_id lookup cache |

#### Key Methods
- **Balance Management:** `get_user_balance()`, `set_user_balance()`, `adjust_user_balance()`
- **Admin Management:** `add_admin()`, `remove_admin()`, `get_all_admins()`
- **Game History:** `add_game_history()`, `get_game_history()`
- **Referrals:** `set_referral_code()`, `get_referrer()`, `update_referral_stats()`
- **Settings Persistence:** `get_casino_bankroll()`, `get_bot_identity()`, `get_all_crypto_addresses()`
- **Deposits/Withdrawals:** `create_deposit()`, `mark_deposit_paid()`, `add_withdrawal()`

**Thread Safety:** Uses `threading.RLock()` for all DB access  
**Default DB Path:** `casino_data.db` in same directory

---

### 2. **bot_network.py** - Multi-Bot Management
**Purpose:** Registry & shared blacklist for multiple bot instances  
**Key Features:**

#### Bot Registry (`_NetworkDB` class)
- `add_bot()` - Register new bot (token, name, username, db_path)
- `get_bot_by_token()` / `get_bot_by_name()` - Lookup bots
- `get_all_bots()` - List all registered bots
- `remove_bot()` - Deregister bot

#### Shared Blacklist
- `add_to_blacklist()` - Ban user across network
- `is_blacklisted()` - Check if user is banned
- `get_blacklist()` - Filter by source bot or reason
- `export_blacklist_csv()` - Export blacklist data

#### Utility Functions
```python
validate_bot_token(token)        # Verify token with Telegram API
ping_bot(token)                  # Check online status & latency
sync_settings_to_bot()           # Copy settings between bot DBs
crossban_user_on_bot()           # Ban user on another bot's DB
get_all_user_ids_from_bot()      # Fetch all user IDs from bot
get_bot_stats()                  # Aggregate stats (users, games, profit)
```

**Database Path:** `bot_network.db` (local SQLite)  
**Network DB Location:** Uses `_conn()` function for lazy connection

---

### 3. **languages.py** - Internationalization
**Purpose:** Multi-language support for bot UI  
**Supported Languages:** English (en), Russian (ru), German (de), French (fr), Chinese (zh)

#### Key Functions
- `detect_lang(language_code)` - Map Telegram language_code → supported lang (fallback: en)
- `get_lang_string(key, lang)` - Retrieve translation (fallback to key if missing)

**Language Strings:** Stored in `LANG_STRINGS` dict (extensible per-language JSON)  
**Auto-Detection:** Based on Telegram user's language_code

---

### 4. **oxapay.py** - Crypto Payment Gateway
**Purpose:** OxaPay integration for crypto deposits  
**Status:** Stub implementation (production requires API keys)

#### Supported Cryptocurrencies
| Coin | Network |
|------|---------|
| USDT | TRC20 |
| BTC | BTC |
| ETH | ERC20 |
| LTC | LTC |
| DOGE | DOGE |

#### API Functions
```python
get_crypto_amount_for_usd(usd_amount, currency)  # Rate conversion
create_invoice(amount, currency, ...)            # Generate payment invoice
request_static_address(currency, network)        # Get deposit address
check_invoice(track_id)                          # Poll payment status
```

**Current State:** Returns safe fallbacks; production deployment needs:
- OxaPay merchant API key
- Webhook configuration for payment callbacks
- Rate fetch implementation (currently hardcoded 1:1)

---

### 5. **casino v5 (1).py** - Main Bot Application
**Purpose:** Core Telegram casino bot with games, deposits, withdrawals, and admin panel  
**Lines:** ~3,000+ (very large file - needs modularization)

#### 🎮 Game Systems

##### Point-Based Games (Dice, Darts, Football, Basketball, Bowling)
- **Mechanics:** Predict outcome between min/max values
- **Multiplier:** 1.92x (house edge built-in)
- **Bet Flow:** User selects game → sets bet → gets result with win/loss calculation

##### Mines Game
- **Type:** Cascading probability game
- **Session Storage:** `mines_games` dict (user_id → MinesGame instance)

##### Coinflip Game
- **Type:** Binary prediction (heads/tails)
- **Assets:** Custom sticker file support (`coinflip_stickers.json`)
- **Multiplier:** 1.92x
- **Sessions:** `coinflip_sessions` tracks active games

##### Blackjack Game
- **Type:** Card game vs. house
- **Deck:** Standard 52 cards (A, 2-10, J, Q, K in 4 suits)
- **Bet Options:** 50, 100, 250, 500, 1000 Stars
- **Sessions:** `blackjack_sessions` dict

##### Predict Game
- **Type:** Multi-option prediction
- **House Edge:** 5% built-in (`PREDICT_HOUSE_EDGE`)
- **Default Bet:** 10 Stars, Min: 1 Star

#### 💰 Payment Systems

##### Deposits
- **Methods:** 
  - Crypto (via OxaPay) - USDT, BTC, ETH, LTC, DOGE
  - Telegram Stars (in-app payment)
- **Deposit Bonus:** Multiplier (1x/2x/3x) applied to credited amount
- **Track ID:** Used for OxaPay invoice polling

##### Withdrawals
- **Currency:** Telegram Stars or TON
- **Min Amount:** 200 Stars (configurable via `/wd` command)
- **TX ID Format:** Auto-generated with counter
- **Status Tracking:** Pending → Paid/Failed

#### 👥 User Systems

##### Leveling System (Steel → Diamond V)
- **Tiers:** 26 levels from Steel to Diamond V
- **Progression:** Based on total USD bets (LEVEL_THRESHOLDS)
- **Rewards:**
  - Rakeback % (5% @ Steel → 30% @ Diamond V)
  - Weekly multiplier (1.09x → 3.5x)
  - Level-up bonus Stars (0 → 2500)
- **Calculation:** `total_bets_usd >= threshold` → level up

##### Referral System
- **Code Generation:** Random string per user
- **Earnings:** Share of referred user's losses (stored in `referral_balance`)
- **Tracking:** `user_referrers`, `user_referral_codes`, `referral_stats` in DB

##### Bonus System
- **Sign-up Bonus:** 20-50 Stars (randomized)
- **Weekly Bonus:** Generated on claim, stored with ISO week tracking
- **Deposit Bonus:** Multiplier applied to deposits
- **Golden Hour:** Optional 1.5x multiplier for time-limited periods

#### 🎲 Special Events
- **Jackpot System:** Cumulative Stars fund, first game win claims it
- **Golden Hour:** Time-limited 1.5x win multiplier
- **Cashback Event:** % refund on losing bets during active period
- **Deposit Bonus:** 1x/2x/3x multiplier on credited amounts

#### 🔒 Admin Features
- **Admin List:** Hardcoded `{ADMIN_ID, 8311802199}`
- **Commands:**
  - `/ban` - Ban user across network
  - `/unban` - Remove ban
  - `/wd` - Set min withdrawal amount
  - `/set` - Set crypto addresses (USDT, BTC, ETH, LTC, DOGE)
  - `/cg` - Change gift comment
  - `/steal` - Set bot identity (name, channel, chat, support)
  - `/video` - Set withdrawal video file_id
  - `/stats` - View bot statistics
  - `/gift` - Send real Telegram gift to user

#### 🌐 Bot Identity System
- **Configurable Fields:**
  - Bot name
  - Channel link
  - Chat link
  - Support username
- **Storage:** Persisted in DB `settings` table

#### 📧 Support Ticket System
- **Auto-Generated Ticket IDs:** Sequential counter
- **Topic/Issue Tracking:** User-submitted problem descriptions
- **Status:** Created → (Admin review/resolution)
- **Attachment:** Optional withdrawal TX ID for payment disputes

#### 🎁 Gift System
- **Method:** Telegram native gift feature (real Stars)
- **Admin Mode:** Requires `/pingme` to enable gift capability
- **Gift Messages:** Random messages from predefined list
- **Comment:** Customizable gift comment (set via `/cg`)

#### 🎨 Emoji Customization System
- **Global Emoji Map:** Premium custom emoji replacements (loaded at startup)
- **Pre-seeded:** From "Housebalcasino_by_fStikBot" sticker packs
- **Admin Flow:** `/emoji` command to map new custom emojis
- **Application:** Replaces text emojis in all user-facing messages

#### 🔑 Constants & Configuration
```python
BOT_TOKEN = "8691315259:AAE6Z5HsHN1FBIaEdDVaAuQtLLRJpkjau9A"
PROVIDER_TOKEN = ""  # Empty (Stars payment instead)
ADMIN_ID = 5709159932
BOT_USERNAME = "Librate"
BOT_DB = "bot_data.db"

# Conversion Rates
STARS_TO_USD = 0.0179
STARS_TO_TON = 0.01201014
MIN_WITHDRAWAL = 200

# Game Settings
CF_MULTIPLIER = 1.92  # Coinflip house edge

# UI Pagination
MATCHES_PER_PAGE = 7
MATCH_ID_BASE = 1100000
```

#### 📊 Data Structures

| Variable | Type | Purpose |
|----------|------|---------|
| `user_games` | dict | Active game sessions |
| `mines_games` | dict | Mines game state |
| `user_balances` | defaultdict(float) | User balance cache |
| `game_locks` | defaultdict(asyncio.Lock) | Prevent race conditions |
| `user_profiles` | dict | User profile data cache |
| `user_bonus_claimed` | set | Track bonus claim state |
| `user_weekly_bonus_data` | dict | Weekly bonus per ISO week |
| `banned_users` | set | Ban state tracking |
| `frozen_users` | set | Withdrawal/deposit freeze |
| `active_jackpot_stars` | float | Current jackpot amount |
| `casino_bankroll_usd` | float | Admin-set house bankroll |
| `emoji_map` | dict | Premium emoji substitutions |

---

## 🔄 Data Flow Diagrams

### Deposit Flow
```
User /deposit
  ↓
Select Currency (USDT/BTC/ETH/LTC/DOGE)
  ↓
Enter Amount (USD)
  ↓
OxaPay Invoice Created
  ↓
User Sends Crypto to Address
  ↓
Webhook/Poll: Payment Detected
  ↓
Mark Deposit as "paid" in DB
  ↓
Convert USD → Stars (@ STARS_TO_USD rate)
  ↓
Apply Deposit Bonus Multiplier
  ↓
Credit to User Balance
  ↓
Notify User
```

### Withdrawal Flow
```
User /withdraw
  ↓
Check Balance ≥ MIN_WITHDRAWAL
  ↓
Check User Not Frozen/Banned
  ↓
Select Amount & Destination (Stars/TON)
  ↓
Deduct from Balance
  ↓
Create TX Record (with TX_ID)
  ↓
Store in DB (status: pending)
  ↓
Admin Reviews / Approves
  ↓
Send Telegram Stars to User
  ↓
Update TX Status (paid)
  ↓
Notify User
```

### Game Play Flow
```
User /play [game_type]
  ↓
Show Available Bets
  ↓
User Selects Bet Amount
  ↓
Check Balance ≥ Bet
  ↓
Deduct Bet from Balance
  ↓
Game Result Generated
  ↓
Apply Multiplier (1.92x if win)
  ↓
Add Winnings to Balance
  ↓
Record in game_history DB
  ↓
Update Profile Stats
  ↓
Apply Rakeback (if leveled)
  ↓
Check Jackpot Win
  ↓
Notify User
```

---

## 🔐 Security Considerations

### Current Implementation
✅ Thread-safe DB access (RLock)  
✅ Blacklist system for cross-bot bans  
✅ User/Admin role separation  
✅ Frozen user restrictions (no deposits/withdrawals)  
✅ Banned user restrictions  

### Potential Vulnerabilities
⚠️ **Admin Hardcoding:** Admin IDs hardcoded in main bot file  
⚠️ **Token Exposure:** BOT_TOKEN visible in source code  
⚠️ **No Rate Limiting:** No cooldown on game submissions  
⚠️ **Balance Race Conditions:** In-memory balance dict may desync with DB  
⚠️ **OxaPay Stub:** No real API integration (webhook missing)  
⚠️ **No Input Validation:** Minimal sanitization of user inputs  
⚠️ **Referral Abuse:** No anti-bot protection on referral signup  

### Recommendations
1. Move credentials to `.env` file (use `python-dotenv`)
2. Implement rate limiting per user/chat
3. Add input validation & sanitization
4. Implement webhook for OxaPay payments
5. Add anti-cheat detection for game wins
6. Use cryptographic signatures for bot communications
7. Implement database transaction handling for atomic operations
8. Add audit logging for admin actions

---

## 🎯 Key Features Summary

| Feature | Status | Details |
|---------|--------|---------|
| **Multi-Game Support** | ✅ Active | Dice, Darts, Football, Basketball, Bowling, Mines, Coinflip, Blackjack, Predict |
| **Deposit Methods** | ⚠️ Partial | Crypto (stub only), Telegram Stars (ready) |
| **Withdrawal** | ✅ Active | Stars & TON support |
| **Leveling System** | ✅ Active | 26 tiers with rakeback & multipliers |
| **Referral System** | ✅ Active | Code-based with earnings tracking |
| **Multi-Language** | ✅ Active | EN, RU, DE, FR, ZH |
| **Admin Panel** | ✅ Active | Ban, settings, crypto config, gift sending |
| **Multi-Bot Support** | ✅ Active | Cross-bot blacklist & settings sync |
| **Custom Emojis** | ✅ Active | Premium emoji mapping system |
| **Gift System** | ✅ Active | Telegram native gifts + real Stars |
| **Support Tickets** | ✅ Active | User issue tracking |
| **Analytics** | ✅ Partial | Per-bot stats available |

---

## 🚀 Deployment Architecture

```
┌─────────────────────────────────────────┐
│      Telegram Bot Network                │
│                                          │
│  ┌──────────┐  ┌──────────┐  ┌────────┐ │
│  │ Bot #1   │  │ Bot #2   │  │ Bot N  │ │
│  │(casino   │  │(casino   │  │(casino)│ │
│  │v5.py)    │  │v5.py)    │  │        │ │
│  └──────────┘  └──────────┘  └────────┘ │
│       │              │            │      │
└───────┼──────────────┼────────────┼──────┘
        │              │            │
        └──────────────┬────────────┘
                       │
        ┌──────────────┴──────────────┐
        │                             │
   ┌─────────────┐         ┌──────────────────┐
   │ bot_network │         │   storage.py     │
   │    DB       │         │ (per-bot DB)     │
   │  (shared    │         │                  │
   │ blacklist)  │         │ casino_data.db   │
   └─────────────┘         └──────────────────┘
        │
        │
   ┌────────────────┐
   │  OxaPay API    │ (webhook integration needed)
   │                │
   │ Rate Service   │
   └────────────────┘
```

---

## 📈 Scaling Considerations

### Current Limitations
- **Single Process:** No horizontal scaling
- **In-Memory State:** User games/sessions lost on restart
- **SQLite:** Not suitable for high-concurrency (~100+ concurrent users)
- **API Rate Limits:** No batching for Telegram Bot API

### Recommended Scaling Steps
1. **Database Migration:** SQLite → PostgreSQL (for 1000+ DAU)
2. **Session Persistence:** Redis for game sessions & locks
3. **Message Queue:** Celery/RabbitMQ for async tasks (payments, notifications)
4. **Horizontal Scaling:** Multiple bot instances with load balancer
5. **Caching Layer:** Redis for frequently accessed data (profiles, settings)
6. **API Rate Limiting:** Implement token bucket algorithm per user
7. **Database Sharding:** By user_id for extreme scale

---

## 📝 Code Quality Notes

### Strengths
✅ Modular file organization  
✅ Comprehensive database abstraction  
✅ Thread-safe database access  
✅ Type hints in key functions  
✅ Docstrings on major functions  

### Areas for Improvement
📌 **casino v5.py is ~3000 lines:** Needs modularization into separate modules:
  - `games/` - Game logic separated by type
  - `payments/` - Deposit/withdrawal handlers
  - `admin/` - Admin command handlers
  - `user/` - User profile/settings handlers

📌 **No logging strategy:** Important events not logged (payments, bans, errors)  
📌 **Minimal error handling:** Many try/except blocks return None silently  
📌 **Hardcoded constants:** Move to config file  
📌 **Magic numbers:** Game multipliers, conversion rates should be configurable  

---

## 🔗 Integration Points

### External Services
1. **Telegram Bot API** - All user communications
2. **OxaPay API** - Crypto payment processing (not integrated)
3. **Sticker Bot APIs** - For custom emoji/sticker retrieval

### Data Sources
1. **Telegram Language Codes** - Auto-detect user language
2. **Game Result RNG** - Random number generation (Python `random` module)
3. **User Input** - Bet selections, command parameters

### Data Sinks
1. **SQLite Databases** - All persistent data
2. **Telegram Bot Messages** - User notifications
3. **Telegram Gifts API** - Gift sending (via admin)

---

## 📦 Dependencies

**External Libraries Used:**
```python
telegram                    # Bot API client
telegram.ext               # Handlers & Application
telegram.constants         # ParseMode, etc.
httpx                      # HTTP client for OxaPay
PIL (Pillow)              # Image processing (for rendering game states)
sqlite3                    # Built-in database
asyncio                    # Async/await support
json                       # Data serialization
csv                        # Blacklist export
```

---

## 🎓 Conclusion

This is a **production-grade Telegram casino bot** with sophisticated features including:
- Multi-game support with house edge management
- Crypto and Stars payment integration
- Comprehensive user profiling and leveling
- Multi-bot network with shared controls
- Admin dashboard with analytics

**Primary Use Case:** Telegram-based gambling platform with house advantage  
**Target Audience:** Crypto gaming enthusiasts  
**Monetization:** House edge on game losses, optional withdrawal fees

**Immediate Priority Actions:**
1. Extract bot credentials to environment variables
2. Implement OxaPay webhook for crypto payments
3. Add comprehensive logging
4. Refactor casino v5.py into modules
5. Set up rate limiting & anti-bot detection
6. Deploy to production with PostgreSQL backend
