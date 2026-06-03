# -*- coding: utf-8 -*-
import logging
import random
import string
import re
import json
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Bot, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest, Forbidden, NetworkError
from collections import defaultdict
import asyncio
import sqlite3
import io
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Import SQLite storage layer
from storage import db

# Import multi-language support
from languages import detect_lang, get_lang_string, SUPPORTED_LANGS

# OxaPay crypto payment integration
import oxapay

# Multi-bot network management
from bot_network import (
    network_db, validate_bot_token, ping_bot, detect_db_path_for_token,
    sync_settings_to_bot, crossban_user_on_bot, get_bot_stats,
    get_all_user_ids_from_bot
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "8062106287:AAHuFUn04LihAfyvF8mRCAz7lg_BJRZECCg".strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
PROVIDER_TOKEN = ""
ADMIN_ID = 5709159932

# Bot username for bonus check
BOT_USERNAME = "Librate"

# Data file path
BOT_DB = "bot_data.db"  # SQLite database (fresh start)
DATA_FILE = "bot_data.json"  # JSON data file

# Admin management
admin_list = {ADMIN_ID, 8311802199}
ADMIN_BALANCE = 9999999999

# Streaming message feature (admin-controlled)
streaming_enabled = False  # Toggle for message streaming effect


async def apply_streaming(message_obj, text: str, **kwargs):
    """
    Apply streaming effect to a message.
    If streaming_enabled, sends message in 3-5 word chunks with 150ms delays.
    Otherwise sends the full message at once.
    """
    global streaming_enabled
    
    if not streaming_enabled:
        # Normal mode: send full message
        return await message_obj.reply_html(text, **kwargs)
    
    # Streaming mode: send in chunks
    words = text.split()
    if len(words) <= 5:
        # Too short to stream, send as one
        return await message_obj.reply_html(text, **kwargs)
    
    chunk_size_min, chunk_size_max = 3, 5
    delay_sec = 0.15  # 150ms
    
    messages = []
    i = 0
    while i < len(words):
        chunk_size = random.randint(chunk_size_min, min(chunk_size_max, len(words) - i))
        messages.append(" ".join(words[i:i + chunk_size]))
        i += chunk_size
    
    # Send chunks progressively
    for idx, chunk in enumerate(messages):
        try:
            await message_obj.reply_html(chunk)
            if idx < len(messages) - 1:
                await asyncio.sleep(delay_sec)
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            # Fallback: send remaining as one
            remaining = " ".join(messages[idx:])
            return await message_obj.reply_html(remaining, **kwargs)


user_games = {}
mines_games = {}  # user_id -> MinesGame instance
user_balances = defaultdict(float)  # Kept for backward compatibility, but data is in DB
user_crypto_balances = defaultdict(float)  # Kept for backward compatibility, but data is in DB
# Set admin balance for new admin
user_balances[8311802199] = ADMIN_BALANCE
game_locks = defaultdict(asyncio.Lock)
user_withdrawals = {}  # Kept for backward compatibility, but data is in DB
withdrawal_counter = 26356  # Loaded from DB on startup

user_profiles = {}  # Kept for backward compatibility, but data is in DB
user_game_history = defaultdict(list)  # Kept for backward compatibility, but data is in DB

# Track users who have claimed bonus
user_bonus_claimed = set()

# Track weekly bonus claims (user_id -> last claim datetime)
user_weekly_bonus_claimed = {}

# Track weekly bonus generated amounts per ISO week
# user_id -> {"iso_week": (year, week), "amount_usd": float, "claimed": bool}
user_weekly_bonus_data = {}

# Track last game settings for repeat/double feature
user_last_game_settings = {}

# Username to user_id mapping
username_to_id = {}

# Admin-set casino bankroll (USD)
casino_bankroll_usd = 0.0
_bankroll_win_blocked: set = set()  # user_ids whose last win was blocked by insufficient bankroll

# ── Special Event State ───────────────────────────────────────────────────────
active_jackpot_stars  = 0.0   # >0 = jackpot live; first game win via wrapper claims it
_jackpot_notify_queue = []    # [(user_id, amount)] pending bot notifications
deposit_bonus_mult    = 1     # 1=normal | 2=double | 3=triple — applied on deposit credit
golden_hour_end_dt    = None  # datetime when golden hour ends; None = off
golden_hour_mult_val  = 1.5   # win multiplier during golden hour
cashback_pct          = 0     # 0=off; % of each bet refunded on loss
cashback_end_dt       = None  # datetime when cashback event ends; None = off
cashback_start_dt     = None  # datetime cashback started; used to filter game_history
_cashback_seen_ids    = set() # game_history IDs already refunded (in-memory guard)
# ─────────────────────────────────────────────────────────────────────────────

# Track admin broadcast state per admin user_id
broadcast_waiting = set()

# Track which user owns which menu message (callback protection)
menu_owners = {}

# Withdraw video file_id (set by admin via /video command)
withdraw_video_file_id = None

# Bot identity (set via /steal command)
bot_identity = {
    "name": "Iibrate",
    "channel_link": "",
    "chat_link": "",
    "support_username": ""
}

# Referral system
user_referral_codes = {}  # user_id -> referral_code
referral_code_to_user = {}  # referral_code -> user_id
user_referrers = {}  # user_id -> referrer_user_id (who referred them)
user_referrals = defaultdict(set)  # referrer_user_id -> set of referred user_ids
user_referral_earnings = defaultdict(float)  # user_id -> total lifetime earnings (in stars)
user_referral_balance = defaultdict(float)  # user_id -> current withdrawable balance (in stars)

# Banned users
banned_users = set()  # user_id -> banned status

# Frozen users (can't deposit, withdraw, or play until unfrozen)
frozen_users = set()

# Gift system
admin_gift_mode = {}  # admin_id -> True if pingme was sent (enables real stars gift)
gift_comment = "💰 @Iibrate - be with the best!"  # Gift comment (changeable via /cg)

# Random gift messages for Telegram gifts
GIFT_MESSAGES = [
    "🎉 Surprise! A special gift just for you.",
    "🎂 Lucky player! Enjoy this free casino reward.",
    "💰 Bonus unlocked! Time to play and win.",
    "💎 A gift from the house — good luck!",
    "🔥 You're on a lucky streak! Claim your gift.",
    "💎 Exclusive reward for you. Don't miss it!",
    "🎲 Free coins added — spin now!",
    "⚡ Limited-time gift! Play before it expires.",
    "💰 Winners get rewards — here's yours!",
    "📊 Thanks for playing! Enjoy this bonus."
]

def get_random_gift_message():
    """Get a random gift message from the list"""
    return random.choice(GIFT_MESSAGES)

# Support ticket system
user_tickets = {}  # user_id -> list of tickets
ticket_counter = 1  # Global ticket counter

# Language system
bot_language = "en"  # Default bot language
user_languages = {}  # user_id -> "en"/"ru"/"de"/"fr"/"zh" (user-specific, auto-detected from Telegram language_code)

# Message template system
TEMPLATES_DB = "templates.db"
template_setup_mode = {}  # admin_id -> {"command": "...", "waiting_for": True/False}

# ==================== GLOBAL PREMIUM EMOJI SYSTEM ====================
# Tracks the last bot message per chat for /emoji (extract emojis to map)
last_bot_messages = {}  # chat_id -> {"message_key": str, "text": str, "message_id": int}

# Global emoji map: normal_emoji -> custom_emoji_id. Loaded at startup, applies to ALL users.
emoji_map = {}  # str -> str

# Active /emoji flow per admin (only emojis not yet saved)
emoji_replace_flow = {}  # admin_id -> {"chat_id": int, "emojis": [(char, pos), ...], "current_index": int, "total": int}

EMOJI_DB = "emoji_mappings.db"

# Pre-seeded emoji IDs from Housebalcasino_by_fStikBot + Housebalcasinos_by_fStikBot packs.
# INSERT OR IGNORE on startup so manually-set overrides always take precedence.
PACK_EMOJI_MAP: dict[str, str] = {
    "0️⃣": "6114022787809024775",
    "1️⃣": "6111826942829271640",
    "2️⃣": "6111496921837214536",
    "3️⃣": "6113967352666135290",
    "4️⃣": "6114129328767769624",
    "5️⃣": "6111563214657427973",
    "6️⃣": "6113638298041719612",
    "7️⃣": "6113755567828771200",
    "8️⃣": "6113942798338104243",
    "9️⃣": "6111746150199466816",
    "©️": "6114126674477981913",
    "🃏": "6114122693043296454",
    "🉑": "6111945148919193316",
    "🌐": "6113693642990296264",
    "🌙": "6113896691864181654",
    "🌟": "6113930884098823987",
    "🍏": "6111673191590009480",
    "🍑": "6113664050665627038",
    "🎁": "6113797662803237818",
    "🎉": "6111455466812874411",
    "🎟": "6111922634700627535",
    "🎫": "6111628966311763378",
    "🎯": "6111583254974831218",
    "🎰": "6111493262525077768",
    "🎲": "6113963100648512468",
    "🎳": "6113935045922134733",
    "🏀": "6113650736267010407",
    "🏅": "6114149446394583417",
    "🏆": "6113884210689219626",
    "🏝": "6113669934770822707",
    "🏦": "6114143540814551799",
    "🏪": "6111580171188322502",
    "🏴‍☠️": "6111589280813947800",
    "🐕": "6111421635355482752",
    "🐬": "6111771138319194729",
    "🐳": "6114006368149053409",
    "🐶": "6113670999922712120",
    "🐸": "6111395607853669307",
    "🐹": "6111426248150358900",
    "👅": "6111923759982059219",
    "👊": "6111667633902328232",
    "👋": "6111866662686825703",
    "👍": "6114016310998342604",
    "👎": "6114098598276766482",
    "👑": "6113908949700845406",
    "👛": "6113927924866358642",
    "👤": "6111694314239174008",
    "👥": "6111779268692286630",
    "💃": "6111610755650427825",
    "💎": "6113902223782058655",
    "💖": "6111576980027612117",
    "💙": "6113985090881067770",
    "💠": "6111775772588906842",
    "💡": "6113699647354575994",
    "💤": "6111851127790116810",
    "💬": "6111558619042421818",
    "💰": "6111501702135815626",
    "💲": "6114020223713550008",
    "💳": "6114107475974166922",
    "💸": "6111665933095279128",
    "💻": "6114092134350985741",
    "📈": "6113709195066874926",
    "📉": "6111756737293852039",
    "📌": "6113707623108843767",
    "📎": "6113644646003383301",
    "📖": "6111738024121342598",
    "📚": "6114127043845167948",
    "📞": "6111518246349838229",
    "📢": "6111786651741068237",
    "📣": "6111471431206314677",
    "📤": "6111527166996913059",
    "📥": "6114139434825817835",
    "📰": "6111765275688835886",
    "📶": "6114028238122523697",
    "🔁": "6111809024225713005",
    "🔄": "6111397076732484727",
    "🔎": "6113836613861646277",
    "🔐": "6113827298077579994",
    "🔒": "6111706370212372478",
    "🔗": "6111717665976359348",
    "🔜": "6114091189458181371",
    "🔞": "6113764222187872798",
    "🔥": "6113815177679870968",
    "🔨": "6114151280345618937",
    "🕓": "6113887608008351340",
    "🖼️": "6114062692350172772",
    "🖼": "6111569528259353014",
    "🗑️": "6113973752167406646",
    "🗡": "6111534352477199504",
    "🗣": "6113688398835228390",
    "😊": "6111727694724996003",
    "😔": "6113904659028516214",
    "🙀": "6111742606851449477",
    "🚩": "6111405271530085254",
    "🚰": "6114130161991425566",
    "🛍️": "6111923021247682614",
    "🛒": "6111550883806321922",
    "🛜": "6111621673457294491",
    "🤑": "6111577851905973449",
    "🤖": "6111666246627895590",
    "🤝": "6113908588923590769",
    "🤡": "6113754653000735478",
    "🤥": "6113961605999893153",
    "🥇": "6113868246295780816",
    "🥶": "6113750396688144704",
    "🦋": "6113895016826937538",
    "🦴": "6114168915481342503",
    "🧡": "6114102596891318599",
    "🪙": "6111830812594806438",
    "ℹ️": "6111422155046525368",
    "⌨️": "6111916003271122836",
    "☺️": "6111578491856099942",
    "⚙️": "6111786630266231296",
    "⚠️": "6113689725980121538",
    "⚡": "6114042669212639438",
    "⚽️": "6111397617898363466",
    "⛺️": "6111607238072212936",
    "✅": "6111695280606812565",
    "✈️": "6111930296922283758",
    "❌": "6114018136359443360",
    "❓": "6114071028881693079",
    "❤️": "6111881862576088892",
    "➕": "6111433768638101415",
    "⬆️": "6111595010300321739",
    "⬇️": "6111513178288429946",
    "⭐": "6111461024500555125",
}

# Crypto deposit addresses (set by admin via /set command)
crypto_addresses = {}  # coin_name -> {"address": "...", "network": "..."}
admin_setting_crypto = {}  # admin_id -> coin_name (tracks which coin admin is setting)
# Temporary crypto addresses for users (expires in 1 hour)
user_temp_crypto_addresses = {}  # (user_id, coin_key) -> {"address": "...", "expires_at": datetime}

STARS_TO_USD = 0.0179
STARS_TO_TON = 0.01201014
MIN_WITHDRAWAL = 200  # Can be changed by admin via /wd command
BONUS_AMOUNT = 20  # legacy/profile bonus
BONUS_MIN = 30
BONUS_MAX = 50

# Matches history pagination
MATCHES_PER_PAGE = 7
MATCH_ID_BASE = 1100000  # Base offset for match IDs

# Game display names for /matches
MATCH_GAME_DISPLAY = {
    'dice': {'emoji': '🎲', 'name': 'Dice Battle'},
    'dart': {'emoji': '🎯', 'name': 'Predict'},
    'arrow': {'emoji': '🎯', 'name': 'Predict'},
    'football': {'emoji': '🎯', 'name': 'Predict'},
    'basket': {'emoji': '🎯', 'name': 'Predict'},
    'bowl': {'emoji': '🎯', 'name': 'Predict'},
    'coinflip': {'emoji': '🎲', 'name': 'Coinflip'},
    'mines': {'emoji': '💎', 'name': 'Mines'},
    'predict': {'emoji': '🎲', 'name': 'Predict'},
    'blackjack': {'emoji': '🎴', 'name': 'Blackjack'},
}

# Coinflip game
COINFLIP_STICKERS_FILE = "coinflip_stickers.json"
coinflip_stickers = {"heads": None, "tails": None}  # file_id storage
coinflip_sessions = {}  # user_id -> {"call": "heads"/"tails", "bet": int, "chat_id": int, "message_id": int}
cflip_setup = {}  # admin_id -> {"step": "heads"/"tails"}

CF_MULTIPLIER = 1.92

# ==================== BLACKJACK GAME ====================
blackjack_sessions = {}  # user_id -> session dict
BJ_SUITS = ["♠", "♣", "♥", "♦"]
BJ_VALUES = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
BJ_BET_OPTIONS = [50, 100, 250, 500, 1000]  # Star amounts

GAME_TYPES = {
    'dice': {'emoji': '🎲', 'name': 'Dice', 'max_value': 6, 'icon': '🎲'},
    'bowl': {'emoji': '🎳', 'name': 'Bowling', 'max_value': 6, 'icon': '🎳'},
    'dart': {'emoji': '🎯', 'name': 'Darts', 'max_value': 6, 'icon': '🎯'},
    'arrow': {'emoji': '🎯', 'name': 'Darts', 'max_value': 6, 'icon': '🎯'},
    'football': {'emoji': '⚽', 'name': 'Football', 'max_value': 5, 'icon': '⚽'},
    'basket': {'emoji': '🏀', 'name': 'Basketball', 'max_value': 5, 'icon': '🏀'},
    'coinflip': {'emoji': '🎲', 'name': 'Coinflip', 'max_value': 2, 'icon': '🎲'}
}

# New point-based game system config
GAME_CONFIG = {
    "dice": {
        "emoji": "🎲",
        "name": "Dice game",
        "action": "roll",
        "min": 1,
        "max": 6,
        "tg_emoji": "🎲"
    },
    "dart": {
        "emoji": "🎯",
        "name": "Dart game",
        "action": "throw",
        "min": 1,
        "max": 6,
        "tg_emoji": "🎯"
    },
    "football": {
        "emoji": "⚽",
        "name": "Football game",
        "action": "kick",
        "min": 1,
        "max": 5,
        "tg_emoji": "⚽"
    },
    "basket": {
        "emoji": "🏀",
        "name": "Basket game",
        "action": "shot",
        "min": 1,
        "max": 5,
        "tg_emoji": "🏀"
    },
    "bowl": {
        "emoji": "🎳",
        "name": "Bowling game",
        "action": "score",
        "min": 0,
        "max": 6,
        "tg_emoji": "🎳"
    }
}

MULTIPLIERS = {
    "normal": 1.92,
    "double": 1.92,
    "crazy": 1.92
}

# Game sessions for point-based system (replaces old user_games for dice/dart/football/basket/bowl)
game_sessions = {}

# Predict game sessions
predict_sessions = {}  # user_id -> {"chat_id", "message_id", "selected": set(), "bet": int, "selection_type": str|None}
PREDICT_HOUSE_EDGE = 0.05
PREDICT_DEFAULT_BET = 10
PREDICT_MIN_BET = 1

# Casino Levels System (Steel to Diamond)
CASINO_LEVELS = {
    0: {"name": "Steel", "rakeback": 5.0, "weekly_mult": 1.09, "level_up_bonus": 0, "next_level": 1},
    1: {"name": "Iron I", "rakeback": 6.5, "weekly_mult": 1.09, "level_up_bonus": 5, "next_level": 2},
    2: {"name": "Iron II", "rakeback": 7.0, "weekly_mult": 1.12, "level_up_bonus": 5, "next_level": 3},
    3: {"name": "Iron III", "rakeback": 7.0, "weekly_mult": 1.12, "level_up_bonus": 5, "next_level": 4},
    4: {"name": "Iron IV", "rakeback": 7.0, "weekly_mult": 1.12, "level_up_bonus": 5, "next_level": 5},
    5: {"name": "Bronze I", "rakeback": 7.5, "weekly_mult": 1.15, "level_up_bonus": 7, "next_level": 6},
    6: {"name": "Bronze II", "rakeback": 8.0, "weekly_mult": 1.18, "level_up_bonus": 10, "next_level": 7},
    7: {"name": "Bronze III", "rakeback": 8.5, "weekly_mult": 1.21, "level_up_bonus": 12, "next_level": 8},
    8: {"name": "Bronze IV", "rakeback": 9.0, "weekly_mult": 1.25, "level_up_bonus": 15, "next_level": 9},
    9: {"name": "Silver I", "rakeback": 9.5, "weekly_mult": 1.30, "level_up_bonus": 20, "next_level": 10},
    10: {"name": "Silver II", "rakeback": 10.0, "weekly_mult": 1.35, "level_up_bonus": 25, "next_level": 11},
    11: {"name": "Silver III", "rakeback": 10.5, "weekly_mult": 1.40, "level_up_bonus": 30, "next_level": 12},
    12: {"name": "Silver IV", "rakeback": 11.0, "weekly_mult": 1.45, "level_up_bonus": 40, "next_level": 13},
    13: {"name": "Gold I", "rakeback": 12.0, "weekly_mult": 1.50, "level_up_bonus": 50, "next_level": 14},
    14: {"name": "Gold II", "rakeback": 13.0, "weekly_mult": 1.55, "level_up_bonus": 75, "next_level": 15},
    15: {"name": "Gold III", "rakeback": 14.0, "weekly_mult": 1.60, "level_up_bonus": 100, "next_level": 16},
    16: {"name": "Gold IV", "rakeback": 15.0, "weekly_mult": 1.70, "level_up_bonus": 150, "next_level": 17},
    17: {"name": "Platinum I", "rakeback": 16.0, "weekly_mult": 1.80, "level_up_bonus": 200, "next_level": 18},
    18: {"name": "Platinum II", "rakeback": 17.0, "weekly_mult": 1.90, "level_up_bonus": 250, "next_level": 19},
    19: {"name": "Platinum III", "rakeback": 18.0, "weekly_mult": 2.00, "level_up_bonus": 300, "next_level": 20},
    20: {"name": "Platinum IV", "rakeback": 20.0, "weekly_mult": 2.20, "level_up_bonus": 400, "next_level": 21},
    21: {"name": "Diamond I", "rakeback": 22.0, "weekly_mult": 2.40, "level_up_bonus": 500, "next_level": 22},
    22: {"name": "Diamond II", "rakeback": 24.0, "weekly_mult": 2.60, "level_up_bonus": 750, "next_level": 23},
    23: {"name": "Diamond III", "rakeback": 26.0, "weekly_mult": 2.80, "level_up_bonus": 1000, "next_level": 24},
    24: {"name": "Diamond IV", "rakeback": 28.0, "weekly_mult": 3.00, "level_up_bonus": 1500, "next_level": 25},
    25: {"name": "Diamond V", "rakeback": 30.0, "weekly_mult": 3.50, "level_up_bonus": 2500, "next_level": None}
}

# Level progression thresholds (total bets in USD)
LEVEL_THRESHOLDS = {
    0: 0,      # Steel
    1: 100,    # Iron I
    2: 250,    # Iron II
    3: 500,    # Iron III
    4: 1000,   # Iron IV
    5: 2000,   # Bronze I
    6: 3500,   # Bronze II
    7: 5500,   # Bronze III
    8: 8000,   # Bronze IV
    9: 12000,  # Silver I
    10: 18000, # Silver II
    11: 26000, # Silver III
    12: 36000, # Silver IV
    13: 50000, # Gold I
    14: 70000, # Gold II
    15: 95000, # Gold III
    16: 130000, # Gold IV
    17: 180000, # Platinum I
    18: 250000, # Platinum II
    19: 350000, # Platinum III
    20: 500000, # Platinum IV
    21: 750000, # Diamond I
    22: 1100000, # Diamond II
    23: 1600000, # Diamond III
    24: 2300000, # Diamond IV
    25: 3500000  # Diamond V (MAX)
}


# ==================== SQLITE DATA PERSISTENCE ====================

def save_data():
    """Save all data to SQLite database (now a no-op, data is saved immediately)"""
    # Data is now saved immediately via db module, so this is just for compatibility
    # Some functions may still call save_data() for legacy reasons
    pass


def load_data():
    """Load all data from SQLite database into memory for compatibility"""
    global user_balances, user_profiles, user_game_history, user_bonus_claimed
    global user_withdrawals, withdrawal_counter, admin_list, username_to_id
    global user_last_game_settings, withdraw_video_file_id, casino_bankroll_usd
    global user_weekly_bonus_claimed
    global user_referral_codes, referral_code_to_user, user_referrers
    global user_referrals, user_referral_earnings, user_referral_balance
    global bot_identity, banned_users, frozen_users, MIN_WITHDRAWAL, gift_comment
    global user_tickets, ticket_counter, crypto_addresses, user_crypto_balances, bot_language
    
    try:
        # Initialize database connection (creates tables if needed)
        db.get_db_connection()
        
        # Create backup on startup
        db.backup_database()
        
        # Load data into memory for backward compatibility
        # Note: Most functions now use db directly, but we keep this for compatibility
        
        # Load withdrawal counter
        withdrawal_counter = db.get_withdrawal_counter()
        
        # Load ticket counter
        ticket_counter = db.get_ticket_counter()
        
        # Load min withdrawal
        MIN_WITHDRAWAL = db.get_min_withdrawal()
        
        # Load casino bankroll (seed to 33535.65 on first run)
        casino_bankroll_usd = db.get_casino_bankroll()
        if casino_bankroll_usd == 0.0:
            casino_bankroll_usd = 33535.65
            db.set_casino_bankroll(casino_bankroll_usd)
        
        # Load withdraw video file ID
        withdraw_video_file_id = db.get_withdraw_video_file_id()
        
        # Load bot language
        bot_language = db.get_bot_language()
        
        # Load gift comment
        gift_comment = db.get_gift_comment()
        
        # Load bot identity
        bot_identity.update(db.get_bot_identity())
        
        # Load admins
        admin_list.update(db.get_all_admins())

        # Load frozen users
        frozen_users.update(db.get_frozen_users())

        # Load crypto addresses
        crypto_addresses.update(db.get_all_crypto_addresses())
        
        # Load user balances into memory cache for compatibility
        conn = db.get_db_connection()

        # Load banned users (DB is source of truth for is_banned)
        cursor = conn.execute("SELECT user_id FROM users WHERE is_banned=1")
        for row in cursor.fetchall():
            banned_users.add(int(row["user_id"]))
        cursor = conn.execute("SELECT user_id, balance FROM users")
        for row in cursor.fetchall():
            user_balances[int(row['user_id'])] = float(row['balance'])

        # Load user languages into memory cache
        user_languages.update(db.get_all_user_languages())

        # Load user profiles into memory cache
        cursor = conn.execute("SELECT user_id FROM profiles")
        for row in cursor.fetchall():
            user_id = int(row['user_id'])
            profile = db.get_or_create_profile(user_id)
            user_profiles[user_id] = profile
        
        # Load game history into memory cache
        cursor = conn.execute("SELECT DISTINCT user_id FROM game_history")
        for row in cursor.fetchall():
            user_id = int(row['user_id'])
            user_game_history[user_id] = db.get_game_history(user_id)
        
        # Count users loaded
        user_count = len(user_balances)
        
        logger.info(f"Data loaded successfully from SQLite. Users in database: {user_count}")
        
        # Initialize and load global emoji mappings
        init_emoji_db()
        seed_emoji_map_from_packs()  # Pre-seed Housebalcasino pack IDs (INSERT OR IGNORE)
        load_global_emoji_map()
        logger.info(f"Emoji system ready: {len(emoji_map)} mappings loaded.")
    except Exception as e:
        logger.error(f"Error loading data: {e}")
        raise


class MinesGame:
    def __init__(self, user_id, grid_size, num_mines, bet_amount):
        self.user_id = user_id
        self.grid_size = grid_size
        self.num_mines = num_mines
        self.bet_amount = bet_amount
        self.diamonds_found = 0
        self.opened_tiles = set()  # (row, col) tuples
        self.mines_positions = set()  # (row, col) tuples
        self.game_id = f"{user_id}_{datetime.now().timestamp()}"
        self.game_state = "playing"  # "playing", "cashed_out", "lost"
        self.last_click_time = datetime.now()
        
        # Generate mines randomly
        total_tiles = grid_size * grid_size
        safe_tiles = list(range(total_tiles))
        random.shuffle(safe_tiles)
        self.mines_positions = set()
        for i in range(num_mines):
            row = safe_tiles[i] // grid_size
            col = safe_tiles[i] % grid_size
            self.mines_positions.add((row, col))
    
    def click_tile(self, row, col):
        """Click a tile, return True if diamond, False if mine"""
        if (row, col) in self.opened_tiles:
            return None  # Already opened
        
        self.opened_tiles.add((row, col))
        self.last_click_time = datetime.now()
        
        if (row, col) in self.mines_positions:
            self.game_state = "lost"
            return False  # Hit a mine
        
        self.diamonds_found += 1
        return True  # Found diamond
    
    def calculate_multiplier(self):
        """Calculate current multiplier based on grid size and diamonds found"""
        total_tiles = self.grid_size * self.grid_size
        total_safe = total_tiles - self.num_mines
        multiplier = (total_tiles / total_safe) ** self.diamonds_found
        return round(multiplier, 2)
    
    def get_current_win(self):
        """Get current win amount"""
        multiplier = self.calculate_multiplier()
        return round(self.bet_amount * multiplier)
    
    def cash_out(self):
        """Cash out and end game"""
        self.game_state = "cashed_out"
        return self.get_current_win()


def load_coinflip_stickers():
    global coinflip_stickers
    try:
        with open(COINFLIP_STICKERS_FILE, "r") as f:
            coinflip_stickers.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

def save_coinflip_stickers():
    with open(COINFLIP_STICKERS_FILE, "w") as f:
        json.dump(coinflip_stickers, f)


def is_admin(user_id):
    return db.is_admin(user_id) or user_id in admin_list  # Check both for compatibility


def is_banned(user_id):
    """Check if a user is banned (local DB or shared cross-bot blacklist)"""
    if db.is_user_banned(user_id):
        return True
    try:
        return network_db.is_blacklisted(user_id)
    except Exception:
        return False


def is_frozen(user_id):
    """Check if a user's balance is frozen"""
    return user_id in frozen_users


def get_user_balance(user_id):
    if is_admin(user_id):
        return ADMIN_BALANCE
    return db.get_user_balance(user_id)


# ==================== TRANSLATION SYSTEM ====================

def t(key, **kwargs):
    """Translation function - returns text based on current bot language"""
    translations = {
        "en": {
            # Welcome & Main
            "welcome": "👑 <b>Welcome to {bot_name} Game{admin_badge}</b>\n\n⭐ {bot_name} Game is the best online mini-games on Telegram\n\n📢 <b>How to start winning?</b>\n\n1. Make sure you have a balance. You can top up using the \"Deposit\" button.\n\n2. Join one of our groups from the {bot_name} catalog.\n\n3. Type /play and start playing!\n\n\n💵 Balance: ${balance_usd:.2f}\n👑 Game turnover: ${turnover:.2f}\n\n🌐 <b>About us</b>\n<a href='{channel_link}'>Channel</a> | <a href='{chat_link}'>Chat</a> | <a href='{support_link}'>Support</a>",
            "play_button": "🎮 Play",
            "balance": "Balance",
            "deposit": "Deposit",
            "withdraw": "Withdraw",
            "profile": "Profile",
            "help": "Help",
            "support": "Support",
            
            # Language
            "language_changed_en": "✅ <b>Language changed to English!</b>\n\nThe bot is now using English language.",
            "language_changed_ru": "language_changed_ru",
            
            # Common
            "admin_only": "❌ <b>You don't have permission to use this command.</b>",
            "support_answers": "Support answers in 1—5 minutes.",
            "create_ticket": "✅ Create ticket",
            "my_ticket": "🗒 my ticket",
            "please_use_private": "Please use this command with bot in private messages.",
            "click_here": "Click here",
            
            # Help
            "help_text": "help_text",
            "admin_commands": "👑 <b>Admin Commands:</b>\n/addadmin - Add new admin\n/removeadmin - Remove admin\n/listadmins - View all admins\n/demo - Test games without betting\n/video - Set withdraw video\n/video status - Check video status\n/video remove - Remove video\n/broadcast or /bc - Send a message to all users\n",
            
            # Commands list
            "available_commands": "📋 <b>Available Commands</b>\n\n<b>Basic Commands:</b>\n• /start - Start the bot\n• /help - Show help information\n• /cancel - Cancel current operation\n\n<b>Balance & Money:</b>\n• /balance or /bal - Check your balance\n• /deposit or /depo - Deposit stars\n• /withdraw - Withdraw stars to TON wallet\n\n<b>Games:</b>\n• /play - Start playing games\n\n<b>Profile & Stats:</b>\n• /profile - View your profile\n• /levels - View your level and progress\n• /history - View your game history\n• /leaderboard - View top players\n\n<b>Rewards:</b>\n• /weekly - Claim weekly bonus (Saturdays only)\n• /referral or /ref - View referral information\n\n<b>Social:</b>\n• /tip [amount] - Send stars to another user\n\n<b>Support:</b>\n• /support - Get help or create a support ticket\n\n💡 <b>Tip:</b> Use /help for more information about any command.",
            
            # Balance
            "your_balance": "💰 <b>Your Balance</b>{admin_note}\n\n⭐ Stars: <b>{balance:,} ⭐</b>\n💵 USD: <b>${balance_usd:.2f}</b>",
            "deposit_button": "💳 Deposit",
            "withdraw_button": "💎 Withdraw",
            
            # Deposit
            "select_deposit": "💳 <b>Select deposit amount:</b>",
            "custom_amount": "💳 Custom Amount",
            
            # Withdraw
            "private_command_only": "🔒 <b>Private Command Only</b>\n\nFor your security, the /withdraw command can only be used in a private chat with the bot.\n\n👉 <a href='https://t.me/{bot_username}?start=withdraw'>Click here to open DM</a>\n\nOr search for @{bot_username} and start a private conversation.",
            "welcome_withdraw": "welcome_withdraw",
            "withdraw_button_text": "💎 Withdraw",

            # Main menu / inline (missing keys)
            "menu_choose": "👇 Choose an option:",
            "btn_deposit": "💳 Deposit",
            "btn_withdraw": "💎 Withdraw",
            "btn_balance": "💰 Balance",
            "btn_stats": "📊 Stats",
            "btn_play": "🎮 Play",
            "btn_deposit_inline": "💳 Deposit",
            "btn_withdraw_inline": "💎 Withdraw",
            "back_button": "🔙 Back",
            "back_to_games": "🎮 Back to Games",
            "game_dice": "🎲 Dice",
            "game_bowling": "🎳 Bowling",
            "game_bowl": "🎳 Bowling",
            "game_darts": "🎯 Darts",
            "game_dart": "🎯 Darts",
            "game_football": "⚽ Football",
            "game_basketball": "🏀 Basketball",
            "game_coinflip": "🪙 Coinflip",
            "demo_dice_btn": "🎲 Dice",
            "demo_bowl_btn": "🎳 Bowling",
            "demo_dart_btn": "🎯 Darts",
            "demo_football_btn": "⚽ Football",
            "demo_basketball_btn": "🏀 Basketball",
            "cancel_demo": "❌ Cancel Demo",
            "btn_cancel_demo": "❌ Cancel Demo",
            "mode_normal": "Normal",
            "mode_double": "Double",
            "mode_crazy": "Crazy",
            "cancel_game": "🗑 Cancel",
            "btn_cancel_game": "🗑 Cancel",
            "btn_cancel_game2": "🗑 Cancel",
            "play_again": "🔄 Play Again",
            "btn_play_again": "🔄 Play Again",
            "btn_up_to_1": "First to 1 point",
            "btn_up_to_2": "First to 2 points",
            "btn_up_to_3": "First to 3 points",
            "btn_confirm": "✅ Confirm",
            "btn_cancel": "❌ Cancel",
            "btn_flip_coin": "🪙 Flip!",
            "cancel_button": "❌ Cancel",
            "bj_custom_btn": "✏️ Custom Bet",
            "btn_custom_bet": "✏️ Custom Bet",
            "btn_change_bet": "✏️ Change Bet",
            "pred_active": "⚡ Active Game",
            "btn_all_in": "💰 All In",
            "custom_amount_button": "✏️ Custom Amount",
            "crypto_deposit_button": "💎 Crypto Deposit",
            "withdraw_stars_button": "⭐ Withdraw Stars",
            "withdraw_crypto_button": "💎 Withdraw Crypto",
            "refresh_button": "🔄 Refresh",
            "btn_open_payment": "💳 Open Payment",
            "btn_pay_now": "💳 Pay Now",
            "crypto_bitcoin": "₿ Bitcoin",
            "crypto_ethereum": "Ξ Ethereum",
            "crypto_litecoin": "Ł Litecoin",
            "crypto_solana": "◎ Solana",
            "crypto_ton": "💎 TON",
            "crypto_usdt_bep20": "💵 USDT (BEP20)",
            "crypto_usdc_erc20": "💵 USDC (ERC20)",
            "crypto_monero": "🔒 Monero",
            "oxapay_usdt": "💵 USDT",
            "oxapay_btc": "₿ BTC",
            "oxapay_eth": "Ξ ETH",
            "oxapay_ltc": "Ł LTC",
            "oxapay_doge": "🐕 DOGE",
            "btn_yes": "✅ Yes",
            "btn_no": "❌ No",
            "btn_stars_dep": "⭐ Stars",
            "btn_crypto_dep": "💎 Crypto",
            "btn_confirm_sync": "✅ Confirm Sync",
            "redeem_bonus": "🎂 Redeem Bonus",
            "claim_bonus_locked": "🔒 Bonus Locked",
        },
        "ru": {
            # Welcome & Main
            "welcome": "💎 <b>¢â¬¾±â¢â¬¾ ¿¾¶°»¾²°â¢â¬Å¡ââ ² {bot_name} ¡°·¸½¾{admin_badge}</b>\n\nâ­ {bot_name} - »âââ¢â¬¡ââ ¸µ ¼¸½¸-¸³â¢â¬â¢â¬¹ ² Telegram\n\n📢 <b>¡°º ½°â¢â¬¡°â¢â¬Å¡ââ ²â¢â¬¹¸³â¢â¬â¢â¬¹²°â¢â¬Å¡ââ?</b>\n\n1. £±µ´¸â¢â¬Å¡µâââ, â¢â¬¡â¢â¬Å¡¾ у ²°â µââ¢â¬Å¡ââ ±°»°½â. ¢â¬â¢â¢â¬¹ ¼¾¶µâ¢â¬Å¡µ ¿¾¿¾»½¸â¢â¬Å¡ââ ±°»°½â, ¸â¿¾»ââ·âââ º½¾¿ºââ \"¸¾¿¾»½¸â¢â¬Å¡ââ\".\n\n2. ¸â¢â¬¸â¾µ´¸½â¹â¢â¬Å¡µâââ º ½°ââ ¸¼ ³â¢â¬ââ¿¿°¼ ¸· º°â¢â¬Å¡°»¾³° {bot_name}.\n\n3. ¢â¬â¢²µ´¸â¢â¬Å¡µ /play ¸ ½°â¢â¬¡½¸â¢â¬Å¡µ ¸³â¢â¬°â¢â¬Å¡ââ!\n\n\n💵 ¢â¬Ë°»°½â: ${balance_usd:.2f}\n👑 ¾±¾â¢â¬¾â¢â¬Å¡ ¸³â¢â¬: ${turnover:.2f}\n\nð <b>¾ ½°â</b>\n<a href='{channel_link}'>¡°½°»</a> | <a href='{chat_link}'>§°â¢â¬Å¡</a> | <a href='{support_link}'>¸¾´´µâ¢â¬¶º°</a>",
            "play_button": "play_button",
            "balance": "balance",
            "deposit": "deposit",
            "withdraw": "withdraw",
            "profile": "profile",
            "help": "help",
            "support": "support",
            
            # Language
            "language_changed_en": "✅ <b>Language changed to English!</b>\n\nThe bot is now using English language.",
            "language_changed_ru": "language_changed_ru",
            
            # Common
            "admin_only": "admin_only",
            "support_answers": "support_answers",
            "create_ticket": "create_ticket",
            "my_ticket": "my_ticket",
            "please_use_private": "please_use_private",
            "click_here": "click_here",
            
            # Help
            "help_text": "help_text",
            "admin_commands": "admin_commands",
            
            # Commands list
            "available_commands": "available_commands",
            
            # Balance
            "your_balance": "your_balance",
            "deposit_button": "deposit_button",
            "withdraw_button": "withdraw_button",
            
            # Deposit
            "select_deposit": "select_deposit",
            "custom_amount": "custom_amount",
            
            # Withdraw
            "private_command_only": "private_command_only",
            "welcome_withdraw": "welcome_withdraw",
            "withdraw_button_text": "withdraw_button_text",

            # Main menu / inline (missing keys) — UTF-8; latin-1 decode in t() is a no-op for these
            "menu_choose": "👇 Выберите вариант:",
            "btn_deposit": "💳 Пополнить",
            "btn_stats": "📊 Ð¡Ñ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÐ°",
            "btn_play": "🎮 Играть",
            "btn_deposit_inline": "💳 Пополнить",
            "btn_withdraw_inline": "💎 Ð’Ñ‹Ð²ÐµÑÑ‚Ð¸",
            "back_button": "🔙 ÐÐ°Ð·Ð°Ð´",
            "back_to_games": "🎮 К играм",
            "game_dice": "🎲 ÐšÐ¾ÑÑ‚Ð¸",
            "game_bowling": "🎳 Боулинг",
            "game_bowl": "🎳 Боулинг",
            "game_darts": "🎯 Ð”Ð°Ñ€Ñ‚Ñ",
            "game_dart": "🎯 Ð”Ð°Ñ€Ñ‚Ñ",
            "game_football": "⚽ Футбол",
            "game_basketball": "🏀 Баскетбол",
            "game_coinflip": "🪙 Монетка",
            "demo_dice_btn": "🎲 ÐšÐ¾ÑÑ‚Ð¸",
            "demo_bowl_btn": "🎳 Боулинг",
            "demo_dart_btn": "🎯 Ð”Ð°Ñ€Ñ‚Ñ",
            "demo_football_btn": "⚽ Футбол",
            "demo_basketball_btn": "🏀 Баскетбол",
            "cancel_demo": "âŒ Отменить демо",
            "btn_cancel_demo": "âŒ Отменить демо",
            "mode_normal": "Обычный",
            "mode_double": "Двойной",
            "mode_crazy": "Безумный",
            "cancel_game": "🗑 Отмена",
            "btn_cancel_game": "🗑 Отмена",
            "btn_cancel_game2": "🗑 Отмена",
            "play_again": "🔄 Ещё раз",
            "btn_play_again": "🔄 Ещё раз",
            "btn_up_to_1": "First to 1 point",
            "btn_up_to_2": "First to 2 points",
            "btn_up_to_3": "First to 3 points",
            "btn_confirm": "✅ Подтвердить",
            "btn_cancel": "âŒ Отмена",
            "btn_flip_coin": "btn_flip_coin",
            "cancel_button": "âŒ Отмена",
            "bj_custom_btn": "✏️ Своя ставка",
            "btn_custom_bet": "✏️ Своя ставка",
            "btn_change_bet": "btn_change_bet",
            "pred_active": "⚡ Игра идёт",
            "btn_all_in": "💰 Ва-банк",
            "custom_amount_button": "custom_amount_button",
            "crypto_deposit_button": "💎 Крипто-пополнение",
            "withdraw_stars_button": "â­ Вывод Stars",
            "withdraw_crypto_button": "💎 Вывод крипты",
            "refresh_button": "🔄 Обновить",
            "btn_open_payment": "💳 Открыть оплату",
            "btn_pay_now": "💳 Оплатить",
            "crypto_bitcoin": "₿ Bitcoin",
            "crypto_ethereum": "Ξ Ethereum",
            "crypto_litecoin": "Ł Litecoin",
            "crypto_solana": "◎ Solana",
            "crypto_ton": "💎 TON",
            "crypto_usdt_bep20": "💵 USDT (BEP20)",
            "crypto_usdc_erc20": "💵 USDC (ERC20)",
            "crypto_monero": "🔒 Monero",
            "oxapay_usdt": "💵 USDT",
            "oxapay_btc": "₿ BTC",
            "oxapay_eth": "Ξ ETH",
            "oxapay_ltc": "Ł LTC",
            "oxapay_doge": "🐕 DOGE",
            "btn_yes": "✅ Да",
            "btn_no": "❌ Нет",
            "btn_stars_dep": "⭐ Stars",
            "btn_crypto_dep": "💎 Крипта",
            "btn_confirm_sync": "✅ Подтвердить ÑÐ¸Ð½Ñ…Ñ€Ð¾Ð½Ð¸Ð·Ð°Ñ†Ð¸ÑŽ",
            "redeem_bonus": "redeem_bonus",
            "claim_bonus_locked": "claim_bonus_locked",
            
            # Mines Game
            "mines_title": "mines_title",
            "mines_select_grid": "mines_select_grid",
            "mines_grid_info": "mines_grid_info",
            "mines_select_mines": "mines_select_mines",
            "mines_enter_bet": "mines_enter_bet",
            "mines_game_info": "mines_game_info",
            "mines_grid": "mines_grid",
            "mines_mines": "mines_mines",
            "mines_diamonds_found": "mines_diamonds_found",
            "mines_safe_remaining": "mines_safe_remaining",
            "mines_bet_amount": "mines_bet_amount",
            "mines_current_multiplier": "mines_current_multiplier",
            "mines_potential_win": "mines_potential_win",
            "mines_profit": "mines_profit",
            "mines_cash_out": "mines_cash_out",
            "mines_game_over": "mines_game_over",
            "mines_game_summary": "mines_game_summary",
            "mines_final_multiplier": "mines_final_multiplier",
            "mines_result": "mines_result",
            "mines_hit_bomb": "mines_hit_bomb",
            "mines_cashed_out": "mines_cashed_out",
            "mines_won": "mines_won",
            "mines_congratulations": "mines_congratulations",
            "mines_final_grid": "mines_final_grid",
            "mines_play_again": "mines_play_again",
            "mines_diamond_found": "mines_diamond_found",
            "mines_tile_opened": "mines_tile_opened",
            "mines_game_expired": "mines_game_expired",
            "mines_game_ended": "mines_game_ended",
            "mines_wait": "mines_wait",
            "mines_min_bet": "mines_min_bet",
            "mines_insufficient_balance": "mines_insufficient_balance",
            "mines_shortage": "mines_shortage",
            "mines_invalid_number": "mines_invalid_number",
            "mines_settings_error": "mines_settings_error",
            
            # Crypto
            "crypto_deposit": "crypto_deposit",
            "crypto_withdraw": "crypto_withdraw",
            "crypto_select_coin": "crypto_select_coin",
            "crypto_deposit_title": "crypto_deposit_title",
            "crypto_deposit_instructions": "crypto_deposit_instructions",
            "crypto_address": "crypto_address",
            "crypto_network": "crypto_network",
            "crypto_network_fee": "crypto_network_fee",
            "crypto_temp_address_note": "crypto_temp_address_note",
            "crypto_expires_in": "crypto_expires_in",
            "crypto_refresh": "crypto_refresh",
            "crypto_back": "crypto_back",
            "crypto_enter_withdraw": "crypto_enter_withdraw",
            "crypto_min_withdraw": "crypto_min_withdraw",
            "crypto_balance": "crypto_balance",
            "crypto_withdraw_sent": "crypto_withdraw_sent",
            "crypto_invalid_address": "crypto_invalid_address",
            "crypto_withdraw_summary": "crypto_withdraw_summary",
        }
    }
    translations["en"]["start_info"] = translations["en"]["welcome"]
    translations["ru"]["start_info"] = translations["ru"]["welcome"]

    # Determine user language
    uid = kwargs.get('user_id')
    if uid and uid in user_languages:
        lang = user_languages[uid]
    else:
        lang = "en"

    # 1) Try inline dict (has en + ru with full HTML templates)
    if lang in translations and key in translations[lang]:
        text = translations[lang][key]
    elif key in translations["en"]:
        # Key exists in inline English but not user lang → try external language file
        ext = get_lang_string(key, lang)
        if ext != key:
            text = ext  # found in external file
        else:
            text = translations["en"][key]  # fallback to inline English
    else:
        # Key not in inline dict at all → try external language files
        text = get_lang_string(key, lang)

    # Fix double-encoded UTF-8 (Cyrillic) when Russian was saved as Latin-1
    if lang == "ru":
        try:
            text = text.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass

    # Format with kwargs if provided
    if kwargs:
        try:
            text = text.format(**kwargs)
        except:
            pass

    return text


def translate_text(text, user_id=None):
    """Auto-translate text based on user's detected language.
    Uses language files for de/fr/zh and the legacy inline map for ru."""
    if not text:
        return text

    # Get user's language
    if user_id and user_id in user_languages:
        user_lang = user_languages[user_id]
    else:
        user_lang = "en"

    # No translation needed for English
    if user_lang == "en":
        return text

    # For de/fr/zh — build translation map from language files (en→target)
    if user_lang in ("de", "fr", "zh"):
        from languages import LANG_STRINGS
        en_strings = LANG_STRINGS.get("en", {})
        target_strings = LANG_STRINGS.get(user_lang, {})
        result = text
        # Sort by length descending so longer phrases match first
        for key in sorted(en_strings.keys(), key=lambda k: len(en_strings[k]), reverse=True):
            en_val = en_strings[key]
            tgt_val = target_strings.get(key)
            if tgt_val and en_val in result:
                result = result.replace(en_val, tgt_val)
        return result

    # For Russian — use the legacy inline map (kept for backward compatibility)
    translations_map = {
        # Errors & Permissions
        "You don't have permission": "You don't have permission",
        "Invalid user ID": "Invalid user ID",
        "User not found": "User not found",
        "Cannot ban an admin": "Cannot ban an admin",
        "is already an admin": "is already an admin",
        "is not an admin": "is not an admin",
        "Cannot remove the main admin": "Cannot remove the main admin",
        "Admin only command": "Admin only command",
        "Only admins can": "Only admins can",
        "Use this command in DM": "Use this command in DM",
        
        # Common actions
        "Operation cancelled": "Operation cancelled",
        "Nothing to cancel": "Nothing to cancel",
        "Please enter a valid number": "Please enter a valid number",
        "Bankroll updated": "Bankroll updated",
        "Minimum withdrawal updated": "Minimum withdrawal updated",
        "Please wait": "Please wait",
        "managers will contact you": "managers will contact you",
        "Please send a screen recording": "Please send a screen recording",
        "Your message has been sent": "Your message has been sent",
        "support team": "support team",
        "We will get back to you shortly": "We will get back to you shortly",
        "ticket is linked to exchange": "ticket is linked to exchange",
        
        # Support
        "How did you top up": "How did you top up",
        "stars to your account": "stars to your account",
        "Which bot do you need help with": "Which bot do you need help with",
        "What seems to be the problem": "What seems to be the problem",
        "My transaction is frozen": "My transaction is frozen",
        "My account is locked": "My account is locked",
        "I didn't receive ton": "I didn't receive ton",
        "Another question": "Another question",
        "Hello": "Hello",
        "Select the exchange": "Select the exchange",
        "No withdrawals found": "No withdrawals found",
        "You don't have any withdrawal history": "You don't have any withdrawal history",
        
        # Tips & Balance
        "Tip amount must be at least": "Tip amount must be at least",
        "Invalid user": "Invalid user",
        "You can't tip yourself": "You can't tip yourself",
        "Insufficient balance": "Insufficient balance",
        "Your balance": "Your balance",
        "Tip amount": "Tip amount",
        
        # Admin
        "Please send a valid name": "Please send a valid name",
        "Please send a valid username": "Please send a valid username",
        "No video is currently set": "No video is currently set",
        "Add new admin": "Add new admin",
        "Remove admin": "Remove admin",
        "View all admins": "View all admins",
        "Test games without betting": "Test games without betting",
        "Set withdraw video": "Set withdraw video",
        "Check video status": "Check video status",
        "Remove video": "Remove video",
        "Send a message to all users": "Send a message to all users",
        
        # Games & Play
        "Choose a game": "Choose a game",
        "Select bet amount": "Select bet amount",
        "Choose rounds": "Choose rounds",
        "Choose throws": "Choose throws",
        "Send your emojis": "Send your emojis",
        "Higher total wins": "Higher total wins",
        "Most rounds won": "Most rounds won",
        "Winner takes the pot": "Winner takes the pot",
        
        # Profile & Stats
        "Your profile": "Your profile",
        "View your profile": "View your profile",
        "View your level": "View your level",
        "View your game history": "View your game history",
        "View top players": "View top players",
        "No players yet": "No players yet",
        "Play a game to appear": "Play a game to appear",
        "on the leaderboard": "on the leaderboard",
        
        # Withdraw
        "Welcome to Stars Withdrawal": "Welcome to Stars Withdrawal",
        "Minimum withdrawal": "Minimum withdrawal",
        "Good to know": "Good to know",
        "When you exchange stars": "When you exchange stars",
        "Telegram keeps a 15% fee": "Telegram keeps a 15% fee",
        "applies a 21-day hold": "applies a 21-day hold",
        "We send TON immediately": "We send TON immediately",
        "factoring in this fee": "factoring in this fee",
        "a small service premium": "a small service premium",
        
        # Deposit
        "Select deposit amount": "Select deposit amount",
        "Custom Amount": "Custom Amount",
        
        # Weekly Bonus
        "Weekly Bonus Available": "Weekly Bonus Available",
        "Total estimated Weekly Bonus": "Total estimated Weekly Bonus",
        "Add": "Add",
        "in your name": "in your name",
        "to get your weekly Boosted": "to get your weekly Boosted",
        
        # Referral
        "Your referral code": "Your referral code",
        "Share this code": "Share this code",
        "Referral earnings": "Referral earnings",
        "Referral balance": "Referral balance",
        
        # Broadcast
        "Broadcast Mode": "Broadcast Mode",
        "Send the message": "Send the message",
        "you want to broadcast": "you want to broadcast",
        "Supports text, photos": "Supports text, photos",
        "videos, audio": "videos, audio",
        "documents": "documents",
        "Use /cancel to exit": "Use /cancel to exit",
        "Broadcast finished": "Broadcast finished",
        "Total users": "Total users",
        "Sent": "Sent",
        "Failed": "Failed",
        
        # Cancel
        "Operation cancelled": "Operation cancelled",
        "Nothing to cancel": "Nothing to cancel",
        
        # Error handler
        "An unexpected error occurred": "An unexpected error occurred",
        "Please try again later": "Please try again later",
        "If the problem persists": "If the problem persists",
        "contact support": "contact support",
    }
    
    # Apply translations (case-insensitive where possible)
    result = text
    for eng, rus in translations_map.items():
        # Replace with case preservation
        import re
        pattern = re.compile(re.escape(eng), re.IGNORECASE)
        result = pattern.sub(rus, result)
    
    # Fix double-encoded UTF-8 (Cyrillic) when Russian was saved as Latin-1
    try:
        result = result.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return result


def set_user_balance(user_id, amount):
    if not is_admin(user_id):
        db.set_user_balance(user_id, amount)


def adjust_bankroll_usd(delta_usd: float):
    """Update casino bankroll by delta_usd USD, enforcing $10,000 floor."""
    global casino_bankroll_usd
    new_val = round(casino_bankroll_usd + delta_usd, 2)
    casino_bankroll_usd = max(10000.0, new_val)
    db.set_casino_bankroll(casino_bankroll_usd)


def bankroll_can_pay(payout_stars: int) -> bool:
    """Returns True if the casino bankroll can cover this payout in USD."""
    return casino_bankroll_usd >= round(payout_stars * STARS_TO_USD, 2)


def adjust_user_balance(user_id, amount, game=False):
    global active_jackpot_stars
    if not is_admin(user_id):
        if game:
            if amount > 0:
                # Win: check if bankroll can cover the payout
                payout_usd = round(amount * STARS_TO_USD, 2)
                if casino_bankroll_usd < payout_usd:
                    _bankroll_win_blocked.add(user_id)
                    logger.warning(f"[BANKROLL] Win BLOCKED user={user_id} payout=${payout_usd:.2f} bankroll=${casino_bankroll_usd:.2f}")
                    return False
                _bankroll_win_blocked.discard(user_id)
                adjust_bankroll_usd(-payout_usd)
            else:
                # Loss: bankroll gains the bet amount
                adjust_bankroll_usd(round(-amount * STARS_TO_USD, 2))
        if amount > 0:
            # Golden hour: boost all game wins
            if golden_hour_end_dt and datetime.now() < golden_hour_end_dt:
                amount = int(round(amount * golden_hour_mult_val))
            # Jackpot: first game win claims the pot
            if active_jackpot_stars > 0:
                jackpot_won = int(active_jackpot_stars)
                active_jackpot_stars = 0
                _jackpot_notify_queue.append((user_id, jackpot_won))
                db.adjust_user_balance(user_id, amount + jackpot_won)
                return True
        db.adjust_user_balance(user_id, amount)
    return True


def register_menu_owner(message, owner_id):
    """Register which user owns an inline menu message (chat-scoped)."""
    if message and hasattr(message, "message_id") and hasattr(message, "chat"):
        key = (message.chat_id, message.message_id)
        menu_owners[key] = owner_id


def get_user_link(user_id, name):
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def format_user_display(user_id, profile):
    """Return @username if available, otherwise clickable link with their name."""
    username = (profile.get('username') or '').lstrip('@').strip()
    display_name = profile.get('display_name') or profile.get('username') or 'Player'
    if username and username.lower() != 'unknown':
        return f"@{username}"
    return get_user_link(user_id, display_name)


def build_copy_turn_reply_markup(user_id: int, game_emoji: str):
    """Create a one-tap button that prefills only the game emoji."""
    _ = user_id
    prefill_text = game_emoji
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copy", switch_inline_query_current_chat=prefill_text)]
    ])


def get_or_create_profile(user_id, username=None):
    # Get or create from database
    profile = db.get_or_create_profile(user_id, username)
    
    # Update username mapping if an actual username is provided
    if username:
        db.set_username_mapping(username, user_id)
        username_lower = username.lower().lstrip('@')
        username_to_id[username_lower] = user_id  # Keep in memory for compatibility
    
    # Convert game_counts to defaultdict for compatibility
    if 'game_counts' in profile and not isinstance(profile['game_counts'], defaultdict):
        profile['game_counts'] = defaultdict(int, profile['game_counts'])
    
    # Store in memory cache for backward compatibility
    user_profiles[user_id] = profile
    
    return profile


# ==================== REFERRAL SYSTEM ====================

def generate_referral_code():
    """Generate a unique 8-character referral code"""
    import secrets
    max_attempts = 100
    attempts = 0
    while attempts < max_attempts:
        code = secrets.token_hex(4)[:8]  # 8 characters from hex
        if code not in referral_code_to_user:
            return code
        attempts += 1
    # Fallback: use timestamp-based code if all attempts fail
    import time
    code = hex(int(time.time() * 1000000))[-8:].ljust(8, '0')
    return code


def get_or_create_referral_code(user_id):
    """Get or create a referral code for a user"""
    code = db.get_referral_code(user_id)
    if not code:
        code = generate_referral_code()
        db.set_referral_code(user_id, code)
        # Keep in memory for compatibility
        user_referral_codes[user_id] = code
        referral_code_to_user[code] = user_id
    return code


def get_referral_rate(user_id):
    """Get referral commission rate based on user's level"""
    try:
        profile = get_or_create_profile(user_id)
        total_bets = profile.get('total_bets', 0.0)
        total_bets_usd = total_bets * STARS_TO_USD
        level = get_user_level(total_bets_usd)
        
        # Rate tiers based on level
        if level <= 8:  # Steel to Bronze IV
            return 10.0
        elif level <= 12:  # Silver I to Silver IV
            return 12.0
        elif level <= 20:  # Gold I to Platinum IV
            return 15.0
        else:  # Diamond I to Diamond V
            return 20.0
    except Exception:
        return 10.0  # Default rate


def process_referral_earning(referred_user_id, loss_amount):
    """Process referral earnings when a referred user loses"""
    referrer_id = db.get_referrer(referred_user_id)
    if not referrer_id:
        # Check memory cache for compatibility
        referrer_id = user_referrers.get(referred_user_id)
        if referrer_id:
            db.set_referrer(referred_user_id, referrer_id)
        else:
            return
    
    rate = get_referral_rate(referrer_id)
    earnings = (loss_amount * rate) / 100
    
    # Get current stats
    stats = db.get_referral_stats(referrer_id)
    new_lifetime = stats['lifetime_earnings'] + earnings
    new_balance = stats['withdrawable_balance'] + earnings
    
    # Update in database
    db.update_referral_stats(referrer_id, new_lifetime, new_balance)
    
    # Keep in memory for compatibility
    user_referral_earnings[referrer_id] = new_lifetime
    user_referral_balance[referrer_id] = new_balance
    
    logger.info(f"Referral earning: User {referred_user_id} lost {loss_amount} stars, "
                f"Referrer {referrer_id} earned {earnings} stars ({rate}%)")


# Legacy rank functions (kept for backward compatibility, not used in new level system)
RANKS = {1: {"name": "Newcomer", "xp_required": 0, "emoji": "🌱"}}

def get_user_rank(xp):
    current_rank = 1
    for level, data in RANKS.items():
        if xp >= data['xp_required']:
            current_rank = level
        else:
            break
    return current_rank


def get_rank_info(level):
    return RANKS.get(level, RANKS[1])


def add_xp(user_id, amount):
    profile = get_or_create_profile(user_id)
    profile['xp'] += amount
    save_data()
    return profile['xp']


def update_game_stats(user_id, game_type, bet_amount, win_amount, won):
    profile = get_or_create_profile(user_id)
    
    # Update profile stats
    profile['total_games'] += 1
    profile['total_bets'] += bet_amount
    
    if won:
        profile['games_won'] += 1
        profile['total_wins'] += win_amount
        if win_amount > profile['biggest_win']:
            profile['biggest_win'] = win_amount
    else:
        profile['games_lost'] += 1
        profile['total_losses'] += bet_amount
        # Process referral earnings when user loses
        process_referral_earning(user_id, bet_amount)
    
    profile['game_counts'][game_type] += 1
    
    max_count = 0
    fav_game = None
    for gt, count in profile['game_counts'].items():
        if count > max_count:
            max_count = count
            fav_game = gt
    profile['favorite_game'] = fav_game
    
    # Save to database
    db.update_profile(
        user_id,
        total_games=profile['total_games'],
        total_bets=profile['total_bets'],
        total_wins=profile['total_wins'],
        total_losses=profile['total_losses'],
        games_won=profile['games_won'],
        games_lost=profile['games_lost'],
        favorite_game=profile['favorite_game'],
        biggest_win=profile['biggest_win'],
        game_counts=profile['game_counts']
    )
    
    # Add to game history
    db.add_game_history(user_id, game_type, bet_amount, win_amount if won else 0.0, won)
    
    # Keep in memory for compatibility
    user_game_history[user_id].append({
        'game_type': game_type,
        'bet_amount': bet_amount,
        'win_amount': win_amount if won else 0,
        'won': won,
        'timestamp': datetime.now()
    })


def generate_transaction_id():
    chars = string.ascii_letters + string.digits
    return 'stx' + ''.join(random.choice(chars) for _ in range(80))


def generate_temp_crypto_address(base_address, coin_key):
    """Generate a temporary crypto address based on the base address"""
    # For now, we'll use the base address as-is
    # In a real implementation, you might want to generate sub-addresses
    # For simplicity, we'll append a random suffix to make it unique
    import secrets
    suffix = secrets.token_hex(8)[:16]  # 16 character suffix
    # Format depends on coin type
    if coin_key in ["bitcoin", "litecoin"]:
        # For Bitcoin/Litecoin, we might need a different approach
        # For now, return base address with a note that it's temporary
        return base_address
    elif coin_key in ["ethereum", "usdt_bep20", "usdc_erc20"]:
        # Ethereum addresses are 42 chars, we can't modify them easily
        # Return base address
        return base_address
    elif coin_key == "solana":
        # Solana addresses can be longer
        return base_address
    elif coin_key == "ton":
        # TON addresses are specific format
        return base_address
    elif coin_key == "monero":
        # Monero uses subaddresses
        return base_address
    return base_address


def format_timer(expires_at):
    """Format remaining time as H:MM:SS"""
    from datetime import datetime
    now = datetime.now()
    if expires_at <= now:
        return "0:00:00"
    remaining = expires_at - now
    total_seconds = int(remaining.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def is_valid_crypto_address(address):
    """Validate cryptocurrency address format"""
    if not address:
        return False, "Unknown"
    
    address = address.strip()
    
    # Bitcoin addresses
    # Legacy: starts with 1 or 3, 26-35 chars
    # P2SH: starts with 3
    # Bech32: starts with bc1, 42-62 chars
    if address.startswith('bc1'):
        if 42 <= len(address) <= 62:
            return True, "Bitcoin"
    elif address.startswith('1') or address.startswith('3'):
        if 26 <= len(address) <= 35:
            return True, "Bitcoin"
    
    # Litecoin addresses
    # Legacy: starts with L or M, 26-34 chars
    # Bech32: starts with ltc1, 42-62 chars
    if address.startswith('ltc1'):
        if 42 <= len(address) <= 62:
            return True, "Litecoin"
    elif address.startswith('L') or address.startswith('M'):
        if 26 <= len(address) <= 34:
            return True, "Litecoin"
    
    # Ethereum addresses (42 chars, starts with 0x, hex)
    if address.startswith('0x') and len(address) == 42:
        if re.match(r'^0x[a-fA-F0-9]{40}$', address):
            return True, "Ethereum"
    
    # TON addresses
    ton_pattern = r'^(UQ|EQ|kQ|0Q)[A-Za-z0-9_-]{46}$'
    if re.match(ton_pattern, address):
        return True, "TON"
    # Raw TON format
    raw_pattern = r'^-?[0-9]+:[a-fA-F0-9]{64}$'
    if re.match(raw_pattern, address):
        return True, "TON"
    
    # Solana addresses (base58, 32-44 chars, no 0, O, I, l)
    if 32 <= len(address) <= 44:
        if not re.search(r'[0OIl]', address) and re.match(r'^[1-9A-HJ-NP-Za-km-z]+$', address):
            return True, "Solana"
    
    # Monero addresses (95 or 106 chars, starts with 4)
    if address.startswith('4'):
        if len(address) == 95 or len(address) == 106:
            if re.match(r'^4[0-9A-Za-z]{94,105}$', address):
                return True, "Monero"
    
    # USDT/USDC on Ethereum (same format as Ethereum)
    if address.startswith('0x') and len(address) == 42:
        if re.match(r'^0x[a-fA-F0-9]{40}$', address):
            return True, "USDT"  # Default to USDT for ERC-20
    
    return False, "Unknown"


def detect_coin_from_address(address):
    """Detect cryptocurrency type from address format"""
    is_valid, coin_name = is_valid_crypto_address(address)
    return coin_name


def get_or_create_temp_address(user_id, coin_key, base_address):
    """Get existing temp address or create a new one"""
    from datetime import datetime, timedelta
    key = (user_id, coin_key)
    
    # Check if we have a valid temp address
    if key in user_temp_crypto_addresses:
        temp_data = user_temp_crypto_addresses[key]
        expires_at = temp_data.get("expires_at")
        if expires_at and datetime.now() < expires_at:
            # Still valid, return it
            return temp_data["address"], expires_at
    
    # Create new temp address
    temp_address = generate_temp_crypto_address(base_address, coin_key)
    expires_at = datetime.now() + timedelta(hours=1)
    user_temp_crypto_addresses[key] = {
        "address": temp_address,
        "expires_at": expires_at
    }
    return temp_address, expires_at


def is_valid_ton_address(address):
    if not address:
        return False
    ton_pattern = r'^(UQ|EQ|kQ|0Q)[A-Za-z0-9_-]{46}$'
    if re.match(ton_pattern, address):
        return True
    raw_pattern = r'^-?[0-9]+:[a-fA-F0-9]{64}$'
    if re.match(raw_pattern, address):
        return True
    return len(address) >= 48 and len(address) <= 67


def check_bot_name_in_profile(user) -> bool:
    first_name = (user.first_name or "").lower()
    last_name = (user.last_name or "").lower()
    bot_name_lower = bot_identity.get("name", BOT_USERNAME).lower()
    return bot_name_lower in first_name or bot_name_lower in last_name


def is_private_chat(update: Update) -> bool:
    return update.effective_chat.type == "private"


def save_last_game_settings(user_id, game_type, bet_amount, mode="normal", points_target=1):
    """Save user's last game settings for repeat/double feature"""
    settings = {
        'game_type': game_type,
        'bet_amount': bet_amount,
        'mode': mode,
        'points_target': points_target
    }
    user_last_game_settings[user_id] = settings
    db.set_last_game_settings(user_id, settings)


def get_user_id_by_username(username):
    """Get user_id from username"""
    username_lower = username.lower().lstrip('@')
    return username_to_id.get(username_lower)


# ==================== ERROR HANDLING DECORATOR ====================

def handle_errors(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        # Check if user is banned (allow admins and ban/unban commands)
        user_id = None
        if update.effective_user:
            user_id = update.effective_user.id
        elif update.message and update.message.from_user:
            user_id = update.message.from_user.id
        elif update.callback_query and update.callback_query.from_user:
            user_id = update.callback_query.from_user.id
        
        # Allow ban/unban commands to work even if admin is somehow banned
        command_name = func.__name__
        is_ban_command = command_name in ['ban_command', 'unban_command']
        
        # Check if user is banned (allow admins and ban/unban commands)
        if user_id and is_banned(user_id) and not is_admin(user_id) and not is_ban_command:
            return  # Silently ignore banned users

        # Check if user is frozen (block deposit, withdraw, and game commands)
        frozen_commands = [
            'deposit_command', 'withdraw_command', 'play_command',
            'dice_game', 'dart_game', 'football_game', 'basket_game', 'bowl_game',
            'mines_command', 'predict_command', 'cflip_setup_command', 'cf_command',
            'blackjack_command',  # /bj visual blackjack
        ]
        if user_id and is_frozen(user_id) and not is_admin(user_id) and command_name in frozen_commands:
            if update.message:
                await update.message.reply_html(
                    "🧊 <b>Your account is frozen.</b>\n\n"
                    "You cannot deposit, withdraw, or play until an admin unfreezes your account."
                )
            return

        try:
            return await func(update, context, *args, **kwargs)
        except BadRequest as e:
            logger.error(f"BadRequest in {func.__name__}: {e}")
            try:
                if update.message:
                    await update.message.reply_html(
                        translate_text(
                            "❌ <b>Request Error</b>\n\n"
                            "Something went wrong with your request. Please try again."
                        )
                    )
            except Exception:
                pass
        except Forbidden as e:
            logger.error(f"Forbidden in {func.__name__}: {e}")
        except NetworkError as e:
            logger.error(f"NetworkError in {func.__name__}: {e}")
            try:
                if update.message:
                    await update.message.reply_html(
                        "❌ <b>Network Error</b>\n\n"
                        "Connection issue. Please try again later."
                    )
            except Exception:
                pass
        except TelegramError as e:
            logger.error(f"TelegramError in {func.__name__}: {e}")
            try:
                if update.message:
                    msg_user_id = update.message.from_user.id if update.message.from_user else None
                    await update.message.reply_html(
                        translate_text(
                            "❌ <b>Error</b>\n\n"
                            "An error occurred. Please try again.",
                            user_id=msg_user_id
                        )
                    )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
            try:
                if update.message:
                    msg_user_id = update.message.from_user.id if update.message.from_user else None
                    await update.message.reply_html(
                        translate_text(
                            "❌ <b>Unexpected Error</b>\n\n"
                            "Something went wrong. Please try again later.",
                            user_id=msg_user_id
                        )
                    )
            except Exception:
                pass
    return wrapper


# ==================== BONUS COMMAND ====================

def get_next_saturday():
    """Get the next Saturday at 00:00:00 (if today is Saturday, return next Saturday)"""
    now = datetime.now()
    # Saturday is weekday 5 (Monday=0, Sunday=6)
    days_until_saturday = (5 - now.weekday()) % 7
    
    # If today is Saturday, return next Saturday (7 days)
    if days_until_saturday == 0:
        days_until_saturday = 7
    
    next_saturday = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_saturday += timedelta(days=days_until_saturday)
    return next_saturday


def is_saturday():
    """Check if today is Saturday"""
    return datetime.now().weekday() == 5


def format_time_remaining(target_time):
    """Format time remaining as 'X Days HH:MM:SS'"""
    now = datetime.now()
    if target_time <= now:
        return "0 Days 00:00:00"
    
    delta = target_time - now
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    return f"{days} Days {hours:02d}:{minutes:02d}:{seconds:02d}"


def calculate_estimated_weekly_bonus(user_id):
    """Return a random weekly bonus amount to display (30-50 stars)."""
    return random.randint(BONUS_MIN, BONUS_MAX)


def get_weekly_bonus_amount():
    """Return a random weekly bonus amount within range."""
    return random.randint(BONUS_MIN, BONUS_MAX)


@handle_errors
async def weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    next_saturday = get_next_saturday()
    time_remaining = format_time_remaining(next_saturday)
    estimated_bonus = calculate_estimated_weekly_bonus(user_id)
    
    keyboard = [
        [InlineKeyboardButton(t("redeem_bonus", user_id=user_id), callback_data="redeem_weekly_bonus")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    bot_name = bot_identity.get("name", BOT_USERNAME)
    bonus_text = (
        f"¢° <b>Weekly Bonus Available in {time_remaining}</b>\n\n"
        f"Total estimated Weekly Bonus: {estimated_bonus} ⭐\n\n"
        f"Add @{bot_name} in your name to get your weekly Boosted"
    )
    
    sent = await update.message.reply_html(bonus_text, reply_markup=reply_markup)
    register_menu_owner(sent, user_id)


@handle_errors
async def bonus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Weekly bonus command - available every Saturday, screenshot-accurate UI"""
    user = update.effective_user
    user_id = user.id

    now = datetime.now()
    iso_year, iso_week, _ = now.isocalendar()
    current_iso_week = (iso_year, iso_week)
    is_bonus_day = (now.weekday() == 5)  # Saturday

    # Get or generate bonus amount for this ISO week
    bonus_data = user_weekly_bonus_data.get(user_id)

    if bonus_data and tuple(bonus_data.get("iso_week", ())) == current_iso_week:
        # Same week — reuse existing amount, do NOT regenerate
        bonus_usd = bonus_data["amount_usd"]
        claimed = bonus_data.get("claimed", False)
    else:
        # New week — generate fresh random amount $0.10 — $0.90
        bonus_usd = round(random.uniform(0.10, 0.90), 2)
        claimed = False
        user_weekly_bonus_data[user_id] = {
            "iso_week": current_iso_week,
            "amount_usd": bonus_usd,
            "claimed": claimed,
        }

    is_available = is_bonus_day and not claimed
    bot_name = bot_identity.get("name", BOT_USERNAME)

    if is_available:
        status_line = "⭐ <b>Your weekly bonus is available</b>"
        btn_text = "⭐ Claim bonus"
        btn_data = "claim_weekly_bonus"
    else:
        status_line = "🔒 <b>Your weekly bonus is locked</b>"
        btn_text = "🔒 Claim bonus"
        btn_data = "claim_weekly_bonus_locked"

    text = (
        f"🎂 <b>Receive a bonus every Saturday</b>\n\n"
        f"<i>If you don't claim it during Saturday — it expires</i>\n"
        f"{status_line}\n\n"
        f"<blockquote>Add @{bot_name} to your name and get an extra +10% bonus</blockquote>\n\n"
        f"💰 Your bonus: <b>${bonus_usd:.2f}</b>"
    )

    keyboard = [[InlineKeyboardButton(btn_text, callback_data=btn_data)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await update.message.reply_html(text, reply_markup=reply_markup)
    register_menu_owner(sent, user_id)


@handle_errors
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show referral information and link"""
    try:
        user = update.effective_user
        user_id = user.id
        
        # Check if command is in group chat
        if update.effective_chat.type != "private":
            await update.message.reply_html(
                "Please use this command with bot in private messages."
            )
            return
        
        # Get or create referral code
        ref_code = get_or_create_referral_code(user_id)
        
        # Get referral stats
        rate = get_referral_rate(user_id)
        count = len(user_referrals.get(user_id, set()))
        total_earned = user_referral_earnings.get(user_id, 0.0)
        current_balance = user_referral_balance.get(user_id, 0.0)
        
        # Convert to USD
        total_earned_usd = total_earned * STARS_TO_USD
        current_balance_usd = current_balance * STARS_TO_USD
        
        # Get bot username for link
        try:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username if bot_info.username else "Iibratebot"
        except Exception:
            bot_username = "Iibratebot"  # Fallback
        
        referral_text = (
            f"â¹ï¸  <b>Earn a bonus from the losses of the user you invited</b>\n\n"
            f"🔗 <b>Referral link:</b> t.me/{bot_username}?start=ref-{ref_code}\n"
            f"🔥 <b>Current rate:</b> {rate}%\n"
            f"📈 <b>Users invited:</b> {count}\n"
            f"💵 <b>Total earned:</b> ${total_earned_usd:.2f}\n"
            f"💵 <b>Current referral balance:</b> ${current_balance_usd:.2f}"
        )
        
        await update.message.reply_html(referral_text)
    except Exception as e:
        logger.error(f"Error in referral_command: {e}", exc_info=True)
        await update.message.reply_html(
            translate_text(
                "❌ <b>An error occurred while displaying referral information.</b>\n\n"
                "Please try again later."
            )
        )


# ==================== ADMIN COMMANDS ====================

@handle_errors
async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_html(
            "👑 <b>Add Admin</b>\n\n"
            "Usage: /addadmin [user_id]\n"
            "Example: /addadmin 123456789\n\n"
            f"Current admins: {len(admin_list)}"
        )
        return
    
    try:
        new_admin_id = int(context.args[0])
        
        if new_admin_id in admin_list:
            await update.message.reply_html(translate_text(f"⚠️  User <code>{new_admin_id}</code> is already an admin!", user_id=user_id))
            return
        
        admin_list.add(new_admin_id)
        user_balances[new_admin_id] = ADMIN_BALANCE
        db.add_admin(new_admin_id)
        save_data()
        
        await update.message.reply_html(
            translate_text(
                f"✅ <b>New admin added successfully!</b>\n\n"
                f"👤 User ID: <code>{new_admin_id}</code>\n"
                f"💰 Balance: <b>{ADMIN_BALANCE:,} ⭐</b>\n"
                f"👑 Total admins: {len(admin_list)}"
            )
        )
        
        logger.info(f"Admin {user_id} added new admin: {new_admin_id}")
        
    except ValueError:
        await update.message.reply_html(translate_text("❌ Invalid user ID! Please enter a valid number.", user_id=user_id))


@handle_errors
async def addbal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add balance to user (admin only)"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_html(
            "💰 <b>Add Balance</b>\n\n"
            "Usage: /addbal [user_id/@username] [amount]\n"
            "Example: /addbal 123456789 1000\n"
            "Example: /addbal @username 500\n\n"
            "After sending the command, you'll choose Stars or Crypto."
        )
        return
    
    # Parse arguments
    target_arg = context.args[0]
    amount_arg = context.args[1] if len(context.args) > 1 else None
    
    # Resolve user_id from username or chat_id
    target_user_id = None
    target_username = None
    
    # Check if it's a username (starts with @)
    if target_arg.startswith('@'):
        target_username = target_arg[1:]
        target_user_id = username_to_id.get(target_username.lower())
        if not target_user_id:
            await update.message.reply_html(
                f"❌ <b>User not found!</b>\n\n"
                f"Username: @{target_username}\n\n"
                f"The user must have interacted with the bot first."
            )
            return
    else:
        # Try to parse as user_id
        try:
            target_user_id = int(target_arg)
        except ValueError:
            await update.message.reply_html(translate_text("❌ Invalid user ID or username!", user_id=user_id))
            return
    
    # Get amount if provided
    if amount_arg:
        try:
            amount = float(amount_arg)
            if amount <= 0:
                await update.message.reply_html(translate_text("❌ Amount must be greater than 0!", user_id=user_id))
                return
            
            # Store in context for callback
            context.user_data['addbal_target_id'] = target_user_id
            context.user_data['addbal_amount'] = amount
            context.user_data['addbal_username'] = target_username
            
            # Show buttons to choose Stars or Crypto
            # Use string formatting to ensure proper decimal handling
            amount_str = str(amount).replace('.', 'DOT')  # Replace . with DOT to avoid callback data issues
            keyboard = [
                [
                    InlineKeyboardButton(t("btn_stars_dep", user_id=user_id), callback_data=f"addbal_stars_{target_user_id}_{amount_str}"),
                    InlineKeyboardButton(t("btn_crypto_dep", user_id=user_id), callback_data=f"addbal_crypto_{target_user_id}_{amount_str}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            username_text = f"📛 Username: @{target_username}\n" if target_username else ""
            await update.message.reply_html(
                f"💰 <b>Add Balance</b>\n\n"
                f"👤 User ID: <code>{target_user_id}</code>\n"
                f"{username_text}"
                f"💵 Amount: {amount}\n\n"
                f"Choose balance type:",
                reply_markup=reply_markup
            )
        except ValueError:
            await update.message.reply_html(translate_text("❌ Invalid amount! Please enter a valid number.", user_id=user_id))
    else:
        # No amount provided, ask for it
        context.user_data['addbal_target_id'] = target_user_id
        context.user_data['addbal_username'] = target_username
        context.user_data['waiting_for_addbal_amount'] = True
        
        await update.message.reply_html(
            f"💰 <b>Add Balance</b>\n\n"
            f"👤 User ID: <code>{target_user_id}</code>\n"
            f"📛 Username: @{target_username}" if target_username else f"👤 User ID: <code>{target_user_id}</code>\n\n"
            f"💫 <b>Enter the amount to add:</b>"
        )


@handle_errors
async def removebal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove balance from user (admin only)"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_html(
            "💸 <b>Remove Balance</b>\n\n"
            "Usage: /removebal [user_id/@username] [amount]\n"
            "Example: /removebal 123456789 500\n"
            "Example: /removebal @username 200"
        )
        return
    target_arg = context.args[0]
    target_user_id = None
    target_username = None
    if target_arg.startswith('@'):
        target_username = target_arg[1:]
        target_user_id = username_to_id.get(target_username.lower())
        if not target_user_id:
            await update.message.reply_html(t("user_not_found_user", user_id=user_id, username=target_username))
            return
    else:
        try:
            target_user_id = int(target_arg)
        except ValueError:
            await update.message.reply_html(t("invalid_user_id_or_username", user_id=user_id))
            return
    try:
        amount = float(context.args[1])
        if amount <= 0:
            await update.message.reply_html(t("amount_must_be_positive", user_id=user_id))
            return
    except ValueError:
        await update.message.reply_html(t("invalid_amount", user_id=user_id).rstrip("."))
        return
    if is_admin(target_user_id):
        await update.message.reply_html(t("cannot_modify_admin_balance", user_id=user_id))
        return
    current_balance = db.get_user_balance(target_user_id)
    if amount > current_balance:
        amount = current_balance  # Cap at current balance
    db.adjust_user_balance(target_user_id, -amount)
    new_balance = db.get_user_balance(target_user_id)
    user_balances[target_user_id] = new_balance
    username_display = f"@{target_username}" if target_username else f"<code>{target_user_id}</code>"
    await update.message.reply_html(
        f"💸 <b>Balance Removed!</b>\n\n"
        f"👤 User: {username_display}\n"
        f"➖ Removed: <b>{amount:,.0f} ⭐</b>\n"
        f"💰 New Balance: <b>{new_balance:,.0f} ⭐</b>"
    )
    logger.info(f"Admin {user_id} removed {amount} from user {target_user_id}")


@handle_errors
async def setbal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set user balance to exact amount (admin only)"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_html(
            "💰 <b>Set Balance</b>\n\n"
            "Usage: /setbal [user_id/@username] [amount]\n"
            "Example: /setbal 123456789 1000\n"
            "Example: /setbal @username 500"
        )
        return
    target_arg = context.args[0]
    target_user_id = None
    target_username = None
    if target_arg.startswith('@'):
        target_username = target_arg[1:]
        target_user_id = username_to_id.get(target_username.lower())
        if not target_user_id:
            await update.message.reply_html(t("user_not_found_user", user_id=user_id, username=target_username))
            return
    else:
        try:
            target_user_id = int(target_arg)
        except ValueError:
            await update.message.reply_html(t("invalid_user_id_or_username", user_id=user_id))
            return
    try:
        amount = float(context.args[1])
        if amount < 0:
            await update.message.reply_html(t("amount_negative", user_id=user_id))
            return
    except ValueError:
        await update.message.reply_html(t("invalid_amount", user_id=user_id).rstrip("."))
        return
    if is_admin(target_user_id):
        await update.message.reply_html(t("cannot_modify_admin_balance", user_id=user_id))
        return
    old_balance = db.get_user_balance(target_user_id)
    db.set_user_balance(target_user_id, amount)
    user_balances[target_user_id] = amount
    username_display = f"@{target_username}" if target_username else f"<code>{target_user_id}</code>"
    await update.message.reply_html(
        f"💰 <b>Balance Set!</b>\n\n"
        f"👤 User: {username_display}\n"
        f"📊 Old Balance: <b>{old_balance:,.0f} ⭐</b>\n"
        f"💰 New Balance: <b>{amount:,.0f} ⭐</b>"
    )
    logger.info(f"Admin {user_id} set balance of user {target_user_id} to {amount}")


@handle_errors
async def resetbal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset user balance to zero (admin only)"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    if not context.args or len(context.args) < 1:
        await update.message.reply_html(
            "🔄 <b>Reset Balance</b>\n\n"
            "Usage: /resetbal [user_id/@username]\n"
            "Example: /resetbal 123456789\n"
            "Example: /resetbal @username"
        )
        return
    target_arg = context.args[0]
    target_user_id = None
    target_username = None
    if target_arg.startswith('@'):
        target_username = target_arg[1:]
        target_user_id = username_to_id.get(target_username.lower())
        if not target_user_id:
            await update.message.reply_html(t("user_not_found_user", user_id=user_id, username=target_username))
            return
    else:
        try:
            target_user_id = int(target_arg)
        except ValueError:
            await update.message.reply_html(t("invalid_user_id_or_username", user_id=user_id))
            return
    if is_admin(target_user_id):
        await update.message.reply_html(t("cannot_modify_admin_balance", user_id=user_id))
        return
    old_balance = db.get_user_balance(target_user_id)
    db.set_user_balance(target_user_id, 0)
    user_balances[target_user_id] = 0
    username_display = f"@{target_username}" if target_username else f"<code>{target_user_id}</code>"
    await update.message.reply_html(
        f"🔄 <b>Balance Reset!</b>\n\n"
        f"👤 User: {username_display}\n"
        f"📊 Old Balance: <b>{old_balance:,.0f} ⭐</b>\n"
        f"💰 New Balance: <b>0 ⭐</b>"
    )
    logger.info(f"Admin {user_id} reset balance of user {target_user_id} (was {old_balance})")


@handle_errors
async def transferbal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transfer balance between two users (admin only)"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    if not context.args or len(context.args) < 3:
        await update.message.reply_html(
            "🔄 <b>Transfer Balance</b>\n\n"
            "Usage: /transferbal [from_user] [to_user] [amount]\n"
            "Example: /transferbal 123456789 987654321 500\n"
            "Example: /transferbal @user1 @user2 1000"
        )
        return

    def resolve_user(arg):
        if arg.startswith('@'):
            uname = arg[1:]
            uid = username_to_id.get(uname.lower())
            return uid, uname
        try:
            return int(arg), None
        except ValueError:
            return None, None

    from_id, from_username = resolve_user(context.args[0])
    to_id, to_username = resolve_user(context.args[1])
    if not from_id:
        await update.message.reply_html(t("src_user_not_found", user_id=user_id, arg=context.args[0]))
        return
    if not to_id:
        await update.message.reply_html(t("dst_user_not_found", user_id=user_id, arg=context.args[1]))
        return
    if from_id == to_id:
        await update.message.reply_html(t("cannot_transfer_same_user", user_id=user_id))
        return
    try:
        amount = float(context.args[2])
        if amount <= 0:
            await update.message.reply_html(t("amount_must_be_positive", user_id=user_id))
            return
    except ValueError:
        await update.message.reply_html(t("invalid_amount", user_id=user_id).rstrip("."))
        return
    if is_admin(from_id) or is_admin(to_id):
        await update.message.reply_html(t("cannot_transfer_admin", user_id=user_id))
        return
    from_balance = db.get_user_balance(from_id)
    if amount > from_balance:
        await update.message.reply_html(
            f"❌ <b>Insufficient balance!</b>\n\n"
            f"Source user balance: <b>{from_balance:,.0f} ⭐</b>\n"
            f"Requested transfer: <b>{amount:,.0f} ⭐</b>"
        )
        return
    db.adjust_user_balance(from_id, -amount)
    db.adjust_user_balance(to_id, amount)
    new_from = db.get_user_balance(from_id)
    new_to = db.get_user_balance(to_id)
    user_balances[from_id] = new_from
    user_balances[to_id] = new_to
    from_display = f"@{from_username}" if from_username else f"<code>{from_id}</code>"
    to_display = f"@{to_username}" if to_username else f"<code>{to_id}</code>"
    await update.message.reply_html(
        f"🔄 <b>Balance Transferred!</b>\n\n"
        f"📤 From: {from_display} → <b>{new_from:,.0f} ⭐</b>\n"
        f"📥 To: {to_display} → <b>{new_to:,.0f} ⭐</b>\n"
        f"💰 Amount: <b>{amount:,.0f} ⭐</b>"
    )
    logger.info(f"Admin {user_id} transferred {amount} from {from_id} to {to_id}")


@handle_errors
async def topbal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show top 10 users by balance (admin only)"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    # Fetch more than 10 in case some are admins we need to filter out
    top_users = db.get_top_balances(20)
    # Filter out admins (they have fake unlimited balance)
    top_users = [(uid, bal) for uid, bal in top_users if not is_admin(uid)][:10]
    if not top_users:
        await update.message.reply_html(t("no_users_with_balance", user_id=user_id))
        return
    lines = []
    for i, (uid, balance) in enumerate(top_users, 1):
        # Try to find username
        uname = None
        for name, mapped_id in username_to_id.items():
            if mapped_id == uid:
                uname = name
                break
        display = f"@{uname}" if uname else f"<code>{uid}</code>"
        frozen_tag = " 🧊" if is_frozen(uid) else ""
        lines.append(f"{i}. {display} — <b>{balance:,.0f} ⭐</b>{frozen_tag}")
    text = "🏆 <b>Top 10 Balances</b>\n\n" + "\n".join(lines)
    await update.message.reply_html(text)


@handle_errors
async def totalbal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show total balance across all users (admin only)"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    total = db.get_total_balance()
    conn = db.get_db_connection()
    user_count = conn.execute("SELECT COUNT(*) as cnt FROM users WHERE balance > 0").fetchone()['cnt']
    await update.message.reply_html(
        f"💰 <b>Total Balance Across All Users</b>\n\n"
        f"📊 Total: <b>{total:,.0f} ⭐</b>\n"
        f"👥 Users with balance: <b>{user_count}</b>"
    )


@handle_errors
async def freeze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Freeze a user's balance (admin only)"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    target_user_id = None
    target_username = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username or update.message.reply_to_message.from_user.first_name
    elif context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        if arg.startswith('@'):
            arg = arg[1:]
        if arg.lower() in username_to_id:
            target_user_id = username_to_id[arg.lower()]
            target_username = arg
        else:
            try:
                target_user_id = int(arg)
            except ValueError:
                await update.message.reply_html(
                    "❌ <b>Invalid input!</b>\n\n"
                    "Usage: /freeze [user_id/@username] or reply to a message"
                )
                return
    else:
        await update.message.reply_html(
            "🧊 <b>Freeze User</b>\n\n"
            "Usage:\n"
            "• /freeze [user_id]\n"
            "• /freeze @username\n"
            "• /freeze (reply to user's message)\n\n"
            "Frozen users cannot deposit, withdraw, or play."
        )
        return
    if not target_user_id:
        await update.message.reply_html(t("user_not_found", user_id=user_id))
        return
    if is_admin(target_user_id):
        await update.message.reply_html(t("cannot_freeze_admin", user_id=user_id))
        return
    if target_user_id in frozen_users:
        await update.message.reply_html(t("user_already_frozen", user_id=user_id))
        return
    frozen_users.add(target_user_id)
    db.set_frozen_users(frozen_users)
    balance = get_user_balance(target_user_id)
    username_display = f"@{target_username}" if target_username else f"<code>{target_user_id}</code>"
    await update.message.reply_html(
        f"🧊 <b>User Frozen!</b>\n\n"
        f"👤 User: {username_display}\n"
        f"💰 Frozen Balance: <b>{balance:,.0f} ⭐</b>\n\n"
        f"This user can no longer deposit, withdraw, or play."
    )
    logger.info(f"Admin {user_id} froze user {target_user_id}")


@handle_errors
async def unfreeze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unfreeze a user's balance (admin only)"""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    target_user_id = None
    target_username = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username or update.message.reply_to_message.from_user.first_name
    elif context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        if arg.startswith('@'):
            arg = arg[1:]
        if arg.lower() in username_to_id:
            target_user_id = username_to_id[arg.lower()]
            target_username = arg
        else:
            try:
                target_user_id = int(arg)
            except ValueError:
                await update.message.reply_html(
                    "❌ <b>Invalid input!</b>\n\n"
                    "Usage: /unfreeze [user_id/@username] or reply to a message"
                )
                return
    else:
        await update.message.reply_html(
            "🔥 <b>Unfreeze User</b>\n\n"
            "Usage:\n"
            "• /unfreeze [user_id]\n"
            "• /unfreeze @username\n"
            "• /unfreeze (reply to user's message)"
        )
        return
    if not target_user_id:
        await update.message.reply_html(t("user_not_found", user_id=user_id))
        return
    if target_user_id not in frozen_users:
        await update.message.reply_html(t("user_not_frozen", user_id=user_id))
        return
    frozen_users.discard(target_user_id)
    db.set_frozen_users(frozen_users)
    username_display = f"@{target_username}" if target_username else f"<code>{target_user_id}</code>"
    await update.message.reply_html(
        f"🔥 <b>User Unfrozen!</b>\n\n"
        f"👤 User: {username_display}\n\n"
        f"This user can now deposit, withdraw, and play again."
    )
    logger.info(f"Admin {user_id} unfroze user {target_user_id}")


@handle_errors
async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_html(
            translate_text(
                "👑 <b>Remove Admin</b>\n\n"
                "Usage: /removeadmin [user_id]\n"
                "Example: /removeadmin 123456789"
            )
        )
        return
    
    try:
        remove_admin_id = int(context.args[0])
        
        if remove_admin_id == ADMIN_ID:
            await update.message.reply_html(translate_text("❌ Cannot remove the main admin!", user_id=user_id))
            return
        
        if remove_admin_id not in admin_list:
            await update.message.reply_html(translate_text(f"⚠️  User <code>{remove_admin_id}</code> is not an admin!", user_id=user_id))
            return
        
        admin_list.remove(remove_admin_id)
        db.remove_admin(remove_admin_id)
        save_data()
        
        await update.message.reply_html(
            translate_text(
                f"✅ <b>Admin removed successfully!</b>\n\n"
                f"👤 User ID: <code>{remove_admin_id}</code>\n"
                f"👑 Remaining admins: {len(admin_list)}"
            )
        )
        
        logger.info(f"Admin {user_id} removed admin: {remove_admin_id}")
        
    except ValueError:
        await update.message.reply_html(translate_text("❌ Invalid user ID! Please enter a valid number.", user_id=user_id))


@handle_errors
async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    admin_text = "👑 <b>Admin List</b>\n\n"
    admin_text += f"Total admins: {len(admin_list)}\n\n"
    
    for idx, admin_id in enumerate(admin_list, 1):
        is_main = " (Main Admin)" if admin_id == ADMIN_ID else ""
        admin_text += f"{idx}. <code>{admin_id}</code>{is_main}\n"
    
    await update.message.reply_html(admin_text)


@handle_errors
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user - bot will ignore them"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    target_user_id = None
    target_username = None
    
    # Check if replying to a message
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username or update.message.reply_to_message.from_user.first_name
    # Check if username or user_id provided as argument
    elif context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        # Remove @ if present
        if arg.startswith('@'):
            arg = arg[1:]
        
        # Try to find user by username
        if arg.lower() in username_to_id:
            target_user_id = username_to_id[arg.lower()]
            target_username = arg
        # Try to parse as user_id
        else:
            try:
                target_user_id = int(arg)
            except ValueError:
                await update.message.reply_html(
                    translate_text(
                        "❌ <b>Invalid input!</b>\n\n"
                        "Usage:\n"
                        "• /ban [user_id]\n"
                        "• /ban @username\n"
                        "• /ban (reply to user's message)"
                    )
                )
                return
    else:
        await update.message.reply_html(
            translate_text(
                "🔨 <b>Ban User</b>\n\n"
                "Usage:\n"
                "• /ban [user_id]\n"
                "• /ban @username\n"
                "• /ban (reply to user's message)\n\n"
                "Example: /ban 123456789 or /ban @username",
                user_id=user_id
            )
        )
        return
    
    if not target_user_id:
        await update.message.reply_html(translate_text("❌ <b>User not found!</b>", user_id=user_id))
        return
    
    # Prevent banning admins
    if is_admin(target_user_id):
        await update.message.reply_html(translate_text("❌ <b>Cannot ban an admin!</b>", user_id=user_id))
        return
    
    # Check if already banned
    if target_user_id in banned_users:
        await update.message.reply_html(
            translate_text(
                f"⚠️  <b>User is already banned!</b>\n\n"
                f"👤 User ID: <code>{target_user_id}</code>\n"
                f"📛 Username: @{target_username}" if target_username else f"👤 User ID: <code>{target_user_id}</code>"
            )
        )
        return
    
    # Ban the user
    banned_users.add(target_user_id)
    db.set_user_banned(target_user_id, True)
    save_data()
    
    # Get user link
    user_link = get_user_link(target_user_id, target_username or f"User {target_user_id}")
    
    await update.message.reply_html(
        translate_text(f"Another one bites the {user_link}..!Banned", user_id=user_id)
    )
    
    logger.info(f"Admin {user_id} banned user: {target_user_id} ({target_username})")


@handle_errors
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unban a user - bot will listen to them again"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    target_user_id = None
    target_username = None
    
    # Check if replying to a message
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username or update.message.reply_to_message.from_user.first_name
    # Check if username or user_id provided as argument
    elif context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        # Remove @ if present
        if arg.startswith('@'):
            arg = arg[1:]
        
        # Try to find user by username
        if arg.lower() in username_to_id:
            target_user_id = username_to_id[arg.lower()]
            target_username = arg
        # Try to parse as user_id
        else:
            try:
                target_user_id = int(arg)
            except ValueError:
                await update.message.reply_html(
                    translate_text(
                        "❌ <b>Invalid input!</b>\n\n"
                        "Usage:\n"
                        "• /unban [user_id]\n"
                        "• /unban @username\n"
                        "• /unban (reply to user's message)"
                    )
                )
                return
    else:
        await update.message.reply_html(
            translate_text(
                "✅ <b>Unban User</b>\n\n"
                "Usage:\n"
                "• /unban [user_id]\n"
                "• /unban @username\n"
                "• /unban (reply to user's message)\n\n"
                "Example: /unban 123456789 or /unban @username"
            )
        )
        return
    
    if not target_user_id:
        await update.message.reply_html(translate_text("❌ <b>User not found!</b>", user_id=user_id))
        return
    
    # Check if user is banned
    if target_user_id not in banned_users:
        await update.message.reply_html(
            f"⚠️  <b>User is not banned!</b>\n\n"
            f"👤 User ID: <code>{target_user_id}</code>\n"
            f"📛 Username: @{target_username}" if target_username else f"👤 User ID: <code>{target_user_id}</code>"
        )
        return
    
    # Unban the user
    banned_users.discard(target_user_id)
    db.set_user_banned(target_user_id, False)
    save_data()
    
    username_display = f"@{target_username}" if target_username else "No username"
    await update.message.reply_html(
        translate_text(
            f"✅ <b>User unbanned successfully!</b>\n\n"
            f"👤 User ID: <code>{target_user_id}</code>\n"
            f"📛 Username: {username_display}\n\n"
            f"The bot will now listen to this user again."
        )
    )
    
    logger.info(f"Admin {user_id} unbanned user: {target_user_id} ({target_username})")


@handle_errors
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all admin commands"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    try:
        total_admins = len(admin_list) if admin_list else 0
    except Exception:
        total_admins = 0
    
    admin_commands_text = (
        "👑 <b>Admin Commands</b>\n\n"
        "<b>Admin Management:</b>\n"
        "• /addadmin [user_id] - Add a new admin\n"
        "• /removeadmin [user_id] - Remove an admin\n"
        "• /listadmins - View all admins\n\n"
        "<b>User Management:</b>\n"
        "• /user - List all users\n"
        "• /ban [user_id/@username] or reply - Ban a user\n"
        "• /unban [user_id/@username] or reply - Unban a user\n"
        "• /freeze [user_id/@username] - Freeze user (no play/deposit/withdraw)\n"
        "• /unfreeze [user_id/@username] - Unfreeze user\n\n"
        "<b>Balance Management:</b>\n"
        "• /addbal [user] [amount] - Add balance\n"
        "• /removebal [user] [amount] - Remove balance\n"
        "• /setbal [user] [amount] - Set exact balance\n"
        "• /resetbal [user] - Reset balance to zero\n"
        "• /transferbal [user1] [user2] [amount] - Transfer balance\n"
        "• /topbal - Top 10 users by balance\n"
        "• /totalbal - Total balance across all users\n\n"
        "<b>Stats:</b>\n"
        "• /today - Dashboard: users, bets, house P/L today vs all time\n\n"
        "<b>Bot Management:</b>\n"
        "• /video - Set withdraw video\n"
        "• /video status - Check video status\n"
        "• /video remove - Remove video\n"
        "• /broadcast or /bc - Send message to all users\n"
        "• /demo - Test games without betting\n"
        "• /steal - Rebrand bot (change name, links, support)\n"
        "• /gift - Send gift to user (emoji or stars)\n"
        "• /cg - Change gift comment\n\n"
        "<b>Bankroll:</b>\n"
        "• /hb or /housebal - Set casino bankroll\n"
        "• /wd - Set minimum withdrawal amount\n\n"
        "<b>Multi-Bot Network:</b>\n"
        "• /addbot [token] - Add bot to network\n"
        "• /removebot [name] - Remove bot from network\n"
        "• /syncbot [token/name] - Sync settings to bot\n"
        "• /syncall - Sync settings to all bots\n"
        "• /crossban [user] - Ban user on all bots\n"
        "• /sharedblacklist - View cross-bot bans\n"
        "• /botnetwork - Network dashboard\n"
        "• /centralstats - Combined stats\n"
        "• /broadcastall - Broadcast to all bots\n\n"
        f"<b>Total Admins:</b> {total_admins}\n"
        f"<b>Your Admin ID:</b> <code>{user_id}</code>"
    )
    
    try:
        await update.message.reply_html(admin_commands_text)
    except Exception as e:
        logger.error(f"Error sending admin command message: {e}", exc_info=True)
        # Try sending as plain text if HTML fails
        try:
            plain_text = (
                "👑 Admin Commands\n\n"
                "Admin Management:\n"
                "• /addadmin [user_id] - Add a new admin\n"
                "• /removeadmin [user_id] - Remove an admin\n"
                "• /listadmins - View all admins\n\n"
                "User Management:\n"
                "• /user - List all users\n"
                "• /ban - Ban a user\n"
                "• /unban - Unban a user\n"
                "• /freeze - Freeze user\n"
                "• /unfreeze - Unfreeze user\n\n"
                "Balance Management:\n"
                "• /addbal - Add balance\n"
                "• /removebal - Remove balance\n"
                "• /setbal - Set exact balance\n"
                "• /resetbal - Reset to zero\n"
                "• /transferbal - Transfer between users\n"
                "• /topbal - Top 10 balances\n"
                "• /totalbal - Total all balances\n\n"
                "Bot Management:\n"
                "• /video - Set withdraw video\n"
                "• /broadcast or /bc - Send message to all users\n"
                "• /demo - Test games without betting\n"
                "• /gift - Send gift to user\n"
                "• /cg - Change gift comment\n\n"
                "Bankroll:\n"
                "• /hb or /housebal - Set casino bankroll\n"
                "• /wd - Set minimum withdrawal amount\n\n"
                "Multi-Bot Network:\n"
                "• /addbot - Add bot to network\n"
                "• /removebot - Remove bot\n"
                "• /syncbot - Sync settings to bot\n"
                "• /syncall - Sync to all bots\n"
                "• /crossban - Ban user on all bots\n"
                "• /sharedblacklist - Cross-bot bans\n"
                "• /botnetwork - Network dashboard\n"
                "• /centralstats - Combined stats\n"
                "• /broadcastall - Broadcast to all bots\n\n"
                f"Total Admins: {total_admins}\n"
                f"Your Admin ID: {user_id}"
            )
            await update.message.reply_text(plain_text)
        except Exception as e2:
            logger.error(f"Error sending plain text admin command: {e2}", exc_info=True)


# ==================== TODAY DASHBOARD (ADMIN) ====================

@handle_errors
async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: Quick stats dashboard for today vs. all time."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("admin_only_simple", user_id=user_id))
        return

    conn = db.get_db_connection()
    today = datetime.now().strftime('%Y-%m-%d')

    # ── Users ──────────────────────────────────────────────────
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active_today = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM game_history WHERE substr(timestamp,1,10)=?",
        (today,)
    ).fetchone()[0]

    # ── Bets today ─────────────────────────────────────────────
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(bet_amount),0), COALESCE(SUM(win_amount),0) "
        "FROM game_history WHERE substr(timestamp,1,10)=?",
        (today,)
    ).fetchone()
    bets_today, wagered_today, payout_today = row[0], row[1], row[2]
    profit_today = wagered_today - payout_today

    # ── All-time ───────────────────────────────────────────────
    row2 = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(bet_amount),0), COALESCE(SUM(win_amount),0) "
        "FROM game_history"
    ).fetchone()
    bets_all, wagered_all, payout_all = row2[0], row2[1], row2[2]
    profit_all = wagered_all - payout_all

    # ── Top game today ─────────────────────────────────────────
    top_row = conn.execute(
        "SELECT game_type, COUNT(*) AS cnt FROM game_history "
        "WHERE substr(timestamp,1,10)=? GROUP BY game_type ORDER BY cnt DESC LIMIT 1",
        (today,)
    ).fetchone()
    top_game = f"{top_row[0]} ({top_row[1]:,} rounds)" if top_row else "—"

    # ── Stars sitting in wallets ───────────────────────────────
    stars_held = conn.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]

    def s(stars: float) -> str:
        return f"{stars:,.0f} ⭐  (${stars * STARS_TO_USD:,.2f})"

    pl_today_icon = "📈" if profit_today >= 0 else "📉"
    pl_all_icon   = "📈" if profit_all   >= 0 else "📉"

    text = (
        f"📊 <b>Dashboard — {today}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>Users</b>\n"
        f"  Registered (all time): <b>{total_users:,}</b>\n"
        f"  Active today: <b>{active_today:,}</b>\n\n"
        f"🎮 <b>Today</b>\n"
        f"  Rounds: <b>{bets_today:,}</b>\n"
        f"  Wagered: <b>{s(wagered_today)}</b>\n"
        f"  Paid out: <b>{s(payout_today)}</b>\n"
        f"  {pl_today_icon} House P/L: <b>{s(profit_today)}</b>\n\n"
        f"📅 <b>All Time</b>\n"
        f"  Rounds: <b>{bets_all:,}</b>\n"
        f"  Wagered: <b>{s(wagered_all)}</b>\n"
        f"  {pl_all_icon} House P/L: <b>{s(profit_all)}</b>\n\n"
        f"💰 Stars in wallets: <b>{s(stars_held)}</b>\n"
        f"🏆 Top game today: <b>{top_game}</b>"
    )
    await update.message.reply_html(text)


# ==================== VIDEO COMMAND (ADMIN) ====================

@handle_errors
async def set_video_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set the withdraw video"""
    global withdraw_video_file_id
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>Admin only command.</b>", user_id=user_id))
        return
    
    # Check if admin wants to view current video status
    if context.args and context.args[0].lower() == 'status':
        if withdraw_video_file_id:
            await update.message.reply_html(
                "🎂¬ <b>Withdraw Video Status</b>\n\n"
                f"✅ Video is set\n"
                f"📎 File ID: <code>{withdraw_video_file_id[:50]}...</code>"
            )
        else:
            await update.message.reply_html(
                "🎂¬ <b>Withdraw Video Status</b>\n\n"
                "❌ No video set yet\n\n"
                "Use /video to set one."
            )
        return
    
    # Check if admin wants to remove video
    if context.args and context.args[0].lower() == 'remove':
        if withdraw_video_file_id:
            withdraw_video_file_id = None
            db.set_withdraw_video_file_id(None)
            await update.message.reply_html(
                "✅ <b>Withdraw video removed!</b>\n\n"
                "The /withdraw command will now send text only."
            )
        else:
            await update.message.reply_html(translate_text("❌ No video is currently set.", user_id=user_id))
        return
    
    context.user_data['waiting_for_video'] = True
    await update.message.reply_html(
        "🎂¬ <b>Set Withdraw Video</b>\n\n"
        "Send a video or MP4 file now.\n\n"
        "This video will be sent with every /withdraw command.\n\n"
        "📍 <b>Other options:</b>\n"
        "• /video status - Check current video\n"
        "• /video remove - Remove current video\n"
        "• /cancel - Cancel this operation"
    )


@handle_errors
async def handle_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle video upload from admin for withdraw feature and support ticket submissions"""
    global withdraw_video_file_id
    user_id = update.effective_user.id
    
    # Check if user has a support ticket waiting for video/mp3
    ticket_id = context.user_data.get('support_waiting_video_ticket_id')
    if ticket_id:
        # Find the ticket
        user_ticket_list = user_tickets.get(user_id, [])
        ticket = None
        for t in user_ticket_list:
            if t.get('ticket_id') == ticket_id and t.get('waiting_for_video'):
                ticket = t
                break
        
        if ticket:
            # Get video/audio/document from message
            video = update.message.video or update.message.animation or update.message.document
            audio = update.message.audio
            
            # Check if it's a video, audio (mp3), or document
            if video or audio:
                # Mark ticket as video received
                ticket['waiting_for_video'] = False
                ticket['video_received'] = True
                save_data()
                
                # Clear the context flag
                context.user_data.pop('support_waiting_video_ticket_id', None)
                
                # Get withdrawal_id for the confirmation message
                withdrawal_id = ticket.get('withdrawal_id')
                
                if withdrawal_id:
                    await update.message.reply_text(
                        translate_text(f"Your message has been sent to the support team. We will get back to you shortly. The ticket is linked to exchange #{withdrawal_id}.", user_id=user_id)
                    )
                else:
                    await update.message.reply_text(
                        translate_text(f"Your message has been sent to the support team. We will get back to you shortly.", user_id=user_id)
                    )
                return
    
    # Only process if admin is waiting to set video
    if not context.user_data.get('waiting_for_video'):
        return
    
    if not is_admin(user_id):
        return
    
    # Get video from message (can be video or animation/GIF)
    video = update.message.video or update.message.animation or update.message.document
    
    if not video:
        await update.message.reply_html(
            "❌ <b>Invalid file!</b>\n\n"
            "Please send a valid video file (MP4, etc.)\n\n"
            "Use /cancel to abort."
        )
        return
    
    # Check if it's a document, verify it's a video type
    if update.message.document:
        mime_type = update.message.document.mime_type or ""
        if not mime_type.startswith('video/'):
            await update.message.reply_html(
                "❌ <b>Invalid file type!</b>\n\n"
                "Please send a video file (MP4, etc.)\n\n"
                "Use /cancel to abort."
            )
            return
    
    global withdraw_video_file_id
    withdraw_video_file_id = video.file_id
    db.set_withdraw_video_file_id(video.file_id)
    context.user_data['waiting_for_video'] = False
    
    await update.message.reply_html(
        "✅ <b>Withdraw video set successfully!</b>\n\n"
        "This video will now be sent with all /withdraw messages.\n\n"
        "📍 <b>Commands:</b>\n"
        "• /video status - Check current video\n"
        "• /video remove - Remove video\n"
        "• /video - Set new video"
    )
    
    logger.info(f"Admin {user_id} set withdraw video: {video.file_id[:50]}...")


# ==================== STEAL COMMAND (ADMIN) ====================

def replace_bot_name_in_text(text, old_name, new_name):
    """Replace bot name in text (case-insensitive)"""
    if not text or not old_name or not new_name:
        return text
    # Replace all occurrences (case-insensitive)
    import re
    pattern = re.compile(re.escape(old_name), re.IGNORECASE)
    return pattern.sub(new_name, text)


@handle_errors
async def steal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to rebrand the bot"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    # Initialize steal flow
    context.user_data['steal_state'] = 'active'
    context.user_data['steal_new_name'] = None
    context.user_data['steal_channel_link'] = None
    context.user_data['steal_chat_link'] = None
    context.user_data['steal_support_username'] = None
    context.user_data['steal_channel_yes'] = False
    context.user_data['steal_chat_yes'] = False
    context.user_data['steal_support_yes'] = False
    
    keyboard = [
        [
            InlineKeyboardButton(translate_text("✅ Yes", user_id=user_id), callback_data="steal_name_yes"),
            InlineKeyboardButton(translate_text("❌ No", user_id=user_id), callback_data="steal_name_no")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_html(
        "🎂­ <b>Bot Rebranding</b>\n\n"
        "This will change the bot's identity:\n"
        "• Bot name (replaces 'Iibrate' everywhere)\n"
        "• Channel link\n"
        "• Chat link\n"
        "• Support username\n\n"
        "📍 <b>Do you want to change the bot name?</b>",
        reply_markup=reply_markup
    )


@handle_errors
async def handle_steal_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle steal command text input flow"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    text = update.message.text.strip()
    steal_state = context.user_data.get('steal_state')
    
    if not steal_state or steal_state not in ['collecting_data', 'collecting_all']:
        return
    
    # Determine which field we're waiting for
    if context.user_data.get('steal_waiting') == 'name':
        if not text or len(text) < 2:
            await update.message.reply_html(translate_text("❌ Please send a valid name (at least 2 characters)", user_id=user_id))
            return
        context.user_data['steal_new_name'] = text
        await update.message.reply_html(translate_text(f"✅ Bot name saved: <b>{text}</b>", user_id=user_id))
        # Move to next value
        await move_to_next_steal_value(update, context)
        return
    
    elif context.user_data.get('steal_waiting') == 'channel':
        if not text.startswith('http://') and not text.startswith('https://') and not text.startswith('@'):
            await update.message.reply_html(
                "❌ Please send a valid channel link or username:\n"
                "• https://t.me/channelname\n"
                "• @channelname"
            )
            return
        context.user_data['steal_channel_link'] = text
        await update.message.reply_html(translate_text(f"✅ Channel link saved: <b>{text}</b>", user_id=user_id))
        # Move to next value
        await move_to_next_steal_value(update, context)
        return
    
    elif context.user_data.get('steal_waiting') == 'chat':
        if not text.startswith('http://') and not text.startswith('https://') and not text.startswith('@'):
            await update.message.reply_html(
                "❌ Please send a valid chat link or username:\n"
                "• https://t.me/chatname\n"
                "• @chatname"
            )
            return
        context.user_data['steal_chat_link'] = text
        await update.message.reply_html(translate_text(f"✅ Chat link saved: <b>{text}</b>", user_id=user_id))
        # Move to next value
        await move_to_next_steal_value(update, context)
        return
    
    elif context.user_data.get('steal_waiting') == 'support':
        if not text or len(text) < 1:
            await update.message.reply_html(translate_text("❌ Please send a valid username", user_id=user_id))
            return
        support_username = text.replace('@', '')
        context.user_data['steal_support_username'] = support_username
        await update.message.reply_html(translate_text(f"✅ Support username saved: <b>@{support_username}</b>", user_id=user_id))
        # Move to next value
        await move_to_next_steal_value(update, context)
        return


async def move_to_next_steal_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Move to the next value that needs to be collected"""
    user_id = update.effective_user.id
    needs_name = context.user_data.get('steal_name_yes') and not context.user_data.get('steal_new_name')
    needs_channel = context.user_data.get('steal_channel_yes') and not context.user_data.get('steal_channel_link')
    needs_chat = context.user_data.get('steal_chat_yes') and not context.user_data.get('steal_chat_link')
    needs_support = context.user_data.get('steal_support_yes') and not context.user_data.get('steal_support_username')
    
    if needs_name:
        context.user_data['steal_waiting'] = 'name'
        await update.message.reply_html(translate_text("📍 <b>Now send the bot name:</b>", user_id=user_id))
    elif needs_channel:
        context.user_data['steal_waiting'] = 'channel'
        await update.message.reply_html(translate_text("📍 <b>Now send the channel link:</b>\n\nFormat: https://t.me/channelname or @channelname", user_id=user_id))
    elif needs_chat:
        context.user_data['steal_waiting'] = 'chat'
        await update.message.reply_html(translate_text("📍 <b>Now send the chat link:</b>\n\nFormat: https://t.me/chatname or @chatname", user_id=user_id))
    elif needs_support:
        context.user_data['steal_waiting'] = 'support'
        await update.message.reply_html(translate_text("📍 <b>Now send the support username:</b> (without @)", user_id=user_id))
    else:
        # All values collected, apply changes
        context.user_data['steal_waiting'] = None
        await apply_steal_changes(update, context)


async def check_and_continue_steal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check if all required data is collected and continue or finish"""
    # This function is now mainly for backward compatibility
    # The main flow uses move_to_next_steal_value
    await move_to_next_steal_value(update, context)


async def apply_steal_changes_from_query(query, context: ContextTypes.DEFAULT_TYPE):
    """Apply all steal changes from a callback query"""
    user_id = query.from_user.id
    old_name = bot_identity.get("name", "Iibrate")
    
    # Update bot name if provided
    if context.user_data.get('steal_new_name'):
        bot_identity["name"] = context.user_data['steal_new_name']
    
    # Update channel link if provided
    if context.user_data.get('steal_channel_link'):
        bot_identity["channel_link"] = context.user_data['steal_channel_link']
    
    # Update chat link if provided
    if context.user_data.get('steal_chat_link'):
        bot_identity["chat_link"] = context.user_data['steal_chat_link']
    
    # Update support username if provided
    if context.user_data.get('steal_support_username'):
        bot_identity["support_username"] = context.user_data['steal_support_username']
    
    db.set_bot_identity(bot_identity)
    
    # Build summary
    new_name = bot_identity.get("name", old_name)
    changes = []
    if context.user_data.get('steal_new_name'):
        changes.append(f"• Name: {old_name} → {new_name}")
    if context.user_data.get('steal_channel_link'):
        changes.append(f"• Channel: {bot_identity.get('channel_link', 'Not set')}")
    if context.user_data.get('steal_chat_link'):
        changes.append(f"• Chat: {bot_identity.get('chat_link', 'Not set')}")
    if context.user_data.get('steal_support_username'):
        changes.append(f"• Support: @{bot_identity.get('support_username', 'Not set')}")
    
    # Clear steal state
    context.user_data.pop('steal_state', None)
    context.user_data.pop('steal_new_name', None)
    context.user_data.pop('steal_channel_link', None)
    context.user_data.pop('steal_chat_link', None)
    context.user_data.pop('steal_support_username', None)
    context.user_data.pop('steal_name_yes', None)
    context.user_data.pop('steal_channel_yes', None)
    context.user_data.pop('steal_chat_yes', None)
    context.user_data.pop('steal_support_yes', None)
    context.user_data.pop('steal_waiting', None)
    
    changes_text = "\n".join(changes) if changes else "No changes made."
    
    await query.message.reply_html(
        f"✅ <b>Bot Rebranding Complete!</b>\n\n"
        f"📍 <b>Changes Applied:</b>\n"
        f"{changes_text}\n\n"
        f"All messages will now use the new identity!"
    )
    
    logger.info(f"Admin {user_id} rebranded bot: {old_name} → {new_name}")


async def apply_steal_changes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply all steal changes"""
    user_id = update.effective_user.id
    old_name = bot_identity.get("name", "Iibrate")
    
    # Update bot name if provided
    if context.user_data.get('steal_new_name'):
        bot_identity["name"] = context.user_data['steal_new_name']
    
    # Update channel link if provided
    if context.user_data.get('steal_channel_link'):
        bot_identity["channel_link"] = context.user_data['steal_channel_link']
    
    # Update chat link if provided
    if context.user_data.get('steal_chat_link'):
        bot_identity["chat_link"] = context.user_data['steal_chat_link']
    
    # Update support username if provided
    if context.user_data.get('steal_support_username'):
        bot_identity["support_username"] = context.user_data['steal_support_username']
    
    db.set_bot_identity(bot_identity)
    
    # Build summary
    new_name = bot_identity.get("name", old_name)
    changes = []
    if context.user_data.get('steal_new_name'):
        changes.append(f"• Name: {old_name} → {new_name}")
    if context.user_data.get('steal_channel_link'):
        changes.append(f"• Channel: {bot_identity.get('channel_link', 'Not set')}")
    if context.user_data.get('steal_chat_link'):
        changes.append(f"• Chat: {bot_identity.get('chat_link', 'Not set')}")
    if context.user_data.get('steal_support_username'):
        changes.append(f"• Support: @{bot_identity.get('support_username', 'Not set')}")
    
    # Clear steal state
    context.user_data.pop('steal_state', None)
    context.user_data.pop('steal_new_name', None)
    context.user_data.pop('steal_channel_link', None)
    context.user_data.pop('steal_chat_link', None)
    context.user_data.pop('steal_support_username', None)
    context.user_data.pop('steal_name_yes', None)
    context.user_data.pop('steal_channel_yes', None)
    context.user_data.pop('steal_chat_yes', None)
    context.user_data.pop('steal_support_yes', None)
    context.user_data.pop('steal_waiting', None)
    
    changes_text = "\n".join(changes) if changes else "No changes made."
    
    # Get message object (could be from update.message or update.callback_query.message)
    message = update.message
    if not message and update.callback_query:
        message = update.callback_query.message
    
    if message:
        await message.reply_html(
            f"✅ <b>Bot Rebranding Complete!</b>\n\n"
            f"📍 <b>Changes Applied:</b>\n"
            f"{changes_text}\n\n"
            f"All messages will now use the new identity!"
        )
    
    logger.info(f"Admin {user_id} rebranded bot: {old_name} → {new_name}")


@handle_errors
async def handle_steal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle steal command inline button callbacks"""
    query = update.callback_query
    if not query:
        return
    
    user_id = query.from_user.id
    data = query.data
    
    if not is_admin(user_id):
        await query.answer(t("err_admin_only_alert", user_id=user_id), show_alert=True)
        return
    
    # Handle name yes/no
    if data == "steal_name_yes":
        context.user_data['steal_name_yes'] = True
        await show_next_steal_question(query, context)
        await query.answer(translate_text("✅ Will change bot name", user_id=user_id))
        return
    
    elif data == "steal_name_no":
        context.user_data['steal_name_yes'] = False
        await show_next_steal_question(query, context)
        await query.answer(t("err_bot_name_skipped", user_id=user_id))
        return
    
    # Handle channel yes/no
    elif data == "steal_channel_yes":
        context.user_data['steal_channel_yes'] = True
        await show_next_steal_question(query, context)
        await query.answer(translate_text("✅ Will change channel link", user_id=user_id))
        return
    
    elif data == "steal_channel_no":
        context.user_data['steal_channel_yes'] = False
        await show_next_steal_question(query, context)
        await query.answer(translate_text("❌ Channel link skipped", user_id=user_id))
        return
    
    # Handle chat yes/no
    elif data == "steal_chat_yes":
        context.user_data['steal_chat_yes'] = True
        await show_next_steal_question(query, context)
        await query.answer(t("info_change_chat_link", user_id=user_id))
        return
    
    elif data == "steal_chat_no":
        context.user_data['steal_chat_yes'] = False
        await show_next_steal_question(query, context)
        await query.answer(translate_text("❌ Chat link skipped", user_id=user_id))
        return
    
    # Handle support yes/no
    elif data == "steal_support_yes":
        context.user_data['steal_support_yes'] = True
        await show_next_steal_question(query, context)
        await query.answer(translate_text("✅ Will change support username", user_id=user_id))
        return
    
    elif data == "steal_support_no":
        context.user_data['steal_support_yes'] = False
        await show_next_steal_question(query, context)
        await query.answer(translate_text("❌ Support username skipped", user_id=user_id))
        return


async def show_next_steal_question(query, context: ContextTypes.DEFAULT_TYPE):
    """Show the next yes/no question in the steal flow"""
    user_id = query.from_user.id
    try:
        if 'steal_name_yes' not in context.user_data:
            # Ask about name
            keyboard = [
                [
                    InlineKeyboardButton(translate_text("✅ Yes", user_id=user_id), callback_data="steal_name_yes"),
                    InlineKeyboardButton(translate_text("❌ No", user_id=user_id), callback_data="steal_name_no")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                translate_text(
                    "🎂­ <b>Bot Rebranding</b>\n\n"
                    "📍 <b>Do you want to change the bot name?</b>\n"
                    "(This replaces 'Iibrate' everywhere)"
                ),
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        elif 'steal_channel_yes' not in context.user_data:
            # Ask about channel
            keyboard = [
                [
                    InlineKeyboardButton(translate_text("✅ Yes", user_id=user_id), callback_data="steal_channel_yes"),
                    InlineKeyboardButton(translate_text("❌ No", user_id=user_id), callback_data="steal_channel_no")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            name_status = "✅ Name: Will change" if context.user_data.get('steal_name_yes') else "❌ Name: Skipped"
            await query.edit_message_text(
                f"{name_status}\n\n{translate_text('📍 <b>Do you want to change the channel link?</b>', user_id=user_id)}",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        elif 'steal_chat_yes' not in context.user_data:
            # Ask about chat
            keyboard = [
                [
                    InlineKeyboardButton(t("btn_yes", user_id=user_id), callback_data="steal_chat_yes"),
                    InlineKeyboardButton(t("btn_no", user_id=user_id), callback_data="steal_chat_no")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            name_status = "✅ Name: Will change" if context.user_data.get('steal_name_yes') else "❌ Name: Skipped"
            channel_status = translate_text("✅ Channel: Will change", user_id=user_id) if context.user_data.get('steal_channel_yes') else translate_text("❌ Channel: Skipped", user_id=user_id)
            await query.edit_message_text(
                f"{name_status}\n{channel_status}\n\n{translate_text('📍 <b>Do you want to change the chat link?</b>', user_id=user_id)}",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        elif 'steal_support_yes' not in context.user_data:
            # Ask about support
            keyboard = [
                [
                    InlineKeyboardButton(translate_text("✅ Yes", user_id=user_id), callback_data="steal_support_yes"),
                    InlineKeyboardButton(translate_text("❌ No", user_id=user_id), callback_data="steal_support_no")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            name_status = "✅ Name: Will change" if context.user_data.get('steal_name_yes') else "❌ Name: Skipped"
            channel_status = translate_text("✅ Channel: Will change", user_id=user_id) if context.user_data.get('steal_channel_yes') else translate_text("❌ Channel: Skipped", user_id=user_id)
            chat_status = translate_text("✅ Chat: Will change", user_id=user_id) if context.user_data.get('steal_chat_yes') else translate_text("❌ Chat: Skipped", user_id=user_id)
            await query.edit_message_text(
                f"{name_status}\n{channel_status}\n{chat_status}\n\n📍 <b>Do you want to change the support username?</b>",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        else:
            # All questions answered, start collecting data
            # Check what values we need to collect
            needs_name = context.user_data.get('steal_name_yes') and not context.user_data.get('steal_new_name')
            needs_channel = context.user_data.get('steal_channel_yes') and not context.user_data.get('steal_channel_link')
            needs_chat = context.user_data.get('steal_chat_yes') and not context.user_data.get('steal_chat_link')
            needs_support = context.user_data.get('steal_support_yes') and not context.user_data.get('steal_support_username')
            
            # If nothing needs to be collected, apply changes
            if not needs_name and not needs_channel and not needs_chat and not needs_support:
                await apply_steal_changes_from_query(query, context)
                return
            
            # Set state to collecting all values
            context.user_data['steal_state'] = 'collecting_all'
            
            # Show summary of what will be collected
            prompt_parts = []
            if needs_name:
                prompt_parts.append("📍 Bot name")
            if needs_channel:
                prompt_parts.append("📍 Channel link")
            if needs_chat:
                prompt_parts.append("📍 Chat link")
            if needs_support:
                prompt_parts.append("📍 Support username")
            
            await query.edit_message_text(
                f"✅ <b>All questions answered!</b>\n\n"
                f"<b>I need the following values:</b>\n" + "\n".join(prompt_parts) + "\n\n"
                f"<b>I'll ask for them one by one. Send the first value now:</b>",
                parse_mode=ParseMode.HTML
            )
            
            # Set waiting state for the first needed value and prompt
            if needs_name:
                context.user_data['steal_waiting'] = 'name'
                await query.message.reply_html(translate_text("📍 <b>Send the bot name:</b>", user_id=user_id))
            elif needs_channel:
                context.user_data['steal_waiting'] = 'channel'
                await query.message.reply_html(f'{t("send_channel_link", user_id=user_id)}\n\n{t("send_channel_format", user_id=user_id)}')
            elif needs_chat:
                context.user_data['steal_waiting'] = 'chat'
                await query.message.reply_html(translate_text("📍 <b>Send the chat link:</b>\n\nFormat: https://t.me/chatname or @chatname", user_id=user_id))
            elif needs_support:
                context.user_data['steal_waiting'] = 'support'
                await query.message.reply_html(translate_text("📍 <b>Send the support username:</b> (without @)", user_id=user_id))
    except Exception as e:
        logger.error(f"Error in show_next_steal_question: {e}")
        try:
            await query.answer(translate_text("❌ An error occurred. Please try again.", user_id=user_id), show_alert=True)
        except:
            pass


@handle_errors
async def handle_broadcast_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture any message from admin when broadcast mode is active."""
    user_id = update.effective_user.id

    # ── Broadcast All (multi-bot) ──
    if context.user_data.get("broadcastall_waiting") and update.effective_chat.type == "private":
        if not is_admin(user_id):
            context.user_data["broadcastall_waiting"] = False
            return
        context.user_data["broadcastall_waiting"] = False

        bots = network_db.get_all_bots()
        total_sent = 0
        total_errors = 0
        total_users = 0

        status_msg = await context.bot.send_message(
            chat_id=user_id, text="📢 Broadcasting to all bots..."
        )

        for bot_info in bots:
            try:
                bot_obj = Bot(token=bot_info["token"])
                user_ids = get_all_user_ids_from_bot(bot_info["db_path"])
                total_users += len(user_ids)
                for uid in user_ids:
                    try:
                        await bot_obj.copy_message(
                            chat_id=uid,
                            from_chat_id=update.message.chat_id,
                            message_id=update.message.message_id
                        )
                        total_sent += 1
                    except (Forbidden, BadRequest):
                        total_errors += 1
                    except Exception:
                        total_errors += 1
                    await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Broadcast to {bot_info['name']} failed: {e}")

        await status_msg.edit_text(
            f"📢 <b>Broadcast All Complete</b>\n\n"
            f"👥 Total users: {total_users:,}\n"
            f"✅ Sent: {total_sent:,}\n"
            f"❌ Failed: {total_errors:,}",
            parse_mode=ParseMode.HTML
        )
        return

    # ── Normal single-bot broadcast ──
    if user_id not in broadcast_waiting:
        return
    if update.effective_chat.type != "private":
        return
    if not is_admin(user_id):
        broadcast_waiting.discard(user_id)
        return

    await perform_broadcast(update, context, update.message)
    broadcast_waiting.discard(user_id)


@handle_errors
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any ongoing operation"""
    user_id = update.effective_user.id
    
    cancelled = False
    
    # Cancel active game session with refund
    if user_id in game_sessions:
        session = game_sessions[user_id]
        if not session.get('is_demo', False) and not is_admin(user_id):
            adjust_user_balance(user_id, session['bet'])
            user_balances[user_id] = get_user_balance(user_id)
        del game_sessions[user_id]
        cancelled = True
    
    # Cancel active predict game (no refund - bet not deducted until play)
    if user_id in predict_sessions:
        del predict_sessions[user_id]
        cancelled = True

    # Cancel active coinflip with refund
    if user_id in coinflip_sessions:
        session = coinflip_sessions[user_id]
        adjust_user_balance(user_id, session['bet'])
        user_balances[user_id] = get_user_balance(user_id)
        del coinflip_sessions[user_id]
        cancelled = True
    
    # Cancel coinflip setup
    if user_id in cflip_setup:
        del cflip_setup[user_id]
        cancelled = True
    
    if context.user_data.get('waiting_for_video'):
        context.user_data['waiting_for_video'] = False
        cancelled = True
    
    if context.user_data.get('waiting_for_custom_amount'):
        context.user_data['waiting_for_custom_amount'] = False
        cancelled = True
    
    if context.user_data.get('withdraw_state'):
        context.user_data['withdraw_state'] = None
        context.user_data['withdraw_amount'] = None
        context.user_data['withdraw_address'] = None
        cancelled = True
    
    # Cancel gift process
    if context.user_data.get('gift_state'):
        context.user_data['gift_state'] = None
        context.user_data['gift_target_user_id'] = None
        context.user_data['gift_target_username'] = None
        cancelled = True

    # Cancel broadcast wait
    if user_id in broadcast_waiting:
        broadcast_waiting.discard(user_id)
        cancelled = True

    # Cancel broadcastall wait
    if context.user_data.get("broadcastall_waiting"):
        context.user_data["broadcastall_waiting"] = False
        cancelled = True

    # Cancel emoji customization flow
    if user_id in emoji_replace_flow:
        del emoji_replace_flow[user_id]
        cancelled = True
    
    if cancelled:
        await update.message.reply_html(translate_text("✅ Operation cancelled."))
    else:
        await update.message.reply_html(translate_text("â¹ï¸  Nothing to cancel."))


@handle_errors
async def tip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message
    
    # Check if using /tip amount @username format
    if context.args and len(context.args) >= 2:
        try:
            tip_amount = int(context.args[0])
            target = context.args[1]
            
            if tip_amount < 1:
                await message.reply_html(translate_text("❌ Tip amount must be at least 1 ⭐", user_id=user_id))
                return
            
            # Check if target is a username
            if target.startswith('@'):
                username = target.lstrip('@')
                recipient_id = get_user_id_by_username(username)
                
                if not recipient_id:
                    await message.reply_html(
                        translate_text(
                            f"❌ <b>User not found!</b>\n\n"
                            f"User @{username} has not interacted with the bot yet.\n"
                            f"They need to use the bot at least once before receiving tips.",
                            user_id=user_id
                        )
                    )
                    return
                
                recipient_profile = user_profiles.get(recipient_id, {})
                recipient_name = recipient_profile.get('username', username)
            else:
                # Try to parse as user_id
                try:
                    recipient_id = int(target)
                    recipient_profile = user_profiles.get(recipient_id, {})
                    recipient_name = recipient_profile.get('username', 'User')
                except ValueError:
                    await message.reply_html(translate_text("❌ Invalid user! Use @username or user ID.", user_id=user_id))
                    return
            
            if recipient_id == user_id:
                await message.reply_html(translate_text("❌ You can't tip yourself!", user_id=user_id))
                return
            
            sender_balance = get_user_balance(user_id)
            if sender_balance < tip_amount:
                await message.reply_html(
                    translate_text(
                        f"❌ <b>Insufficient balance!</b>\n\n"
                        f"Your balance: {sender_balance} ⭐\n"
                        f"Tip amount: {tip_amount} ⭐"
                    )
                )
                return
            
            if not is_admin(user_id):
                adjust_user_balance(user_id, -tip_amount)
                user_balances[user_id] = get_user_balance(user_id)  # Sync memory cache
            
            adjust_user_balance(recipient_id, tip_amount)
            user_balances[recipient_id] = get_user_balance(recipient_id)  # Sync memory cache
            
            tip_usd = tip_amount * STARS_TO_USD
            sender_name = message.from_user.first_name
            
            sender_link = get_user_link(user_id, sender_name)
            recipient_link = get_user_link(recipient_id, recipient_name)
            
            await message.reply_html(
                f"✅ Tipped <b>{tip_amount}⭐</b> to {recipient_link}"
            )
            
            try:
                await context.bot.send_message(
                    chat_id=recipient_id,
                    text=(
                        f"🎂 <b>You received a tip!</b>\n\n"
                        f"👤 From: {sender_link}\n"
                        f"💰 Amount: <b>{tip_amount} ⭐</b> (${tip_usd:.2f})\n\n"
                        f"💵 Your new balance: <b>{get_user_balance(recipient_id)} ⭐</b>"
                    ),
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Could not notify recipient {recipient_id}: {e}")
            
            logger.info(f"Tip: {user_id} ({sender_name}) -> {recipient_id} ({recipient_name}): {tip_amount} stars")
            return
            
        except ValueError:
            pass  # Fall through to reply-based tip
    
    # Reply-based tip
    if not message.reply_to_message:
        await message.reply_html(
            "💵 To transfer, reply to the person's message with /tip &lt;amount&gt;"
        )
        return
    
    if not context.args or len(context.args) == 0:
        await message.reply_html(translate_text("❌ Please specify the amount to tip!\nExample: /tip 100", user_id=user_id))
        return
    
    try:
        tip_amount = int(context.args[0])
        
        if tip_amount < 1:
            await message.reply_html(translate_text("❌ Tip amount must be at least 1 ⭐", user_id=user_id))
            return
        
        recipient_id = message.reply_to_message.from_user.id
        recipient_name = message.reply_to_message.from_user.first_name
        sender_name = message.from_user.first_name
        
        # Update username mapping for recipient
        if message.reply_to_message.from_user.username:
            username_to_id[message.reply_to_message.from_user.username.lower()] = recipient_id
            save_data()
        
        if recipient_id == user_id:
            await message.reply_html(translate_text("❌ You can't tip yourself!", user_id=user_id))
            return
        
        sender_balance = get_user_balance(user_id)
        if sender_balance < tip_amount:
            await message.reply_html(
                f"❌ <b>Insufficient balance!</b>\n\n"
                f"Your balance: {sender_balance} ⭐\n"
                f"Tip amount: {tip_amount} ⭐"
            )
            return
        
        if not is_admin(user_id):
            adjust_user_balance(user_id, -tip_amount)
            user_balances[user_id] = get_user_balance(user_id)  # Sync memory cache
        
        adjust_user_balance(recipient_id, tip_amount)
        get_or_create_profile(recipient_id, recipient_name)
        
        tip_usd = tip_amount * STARS_TO_USD
        
        sender_link = get_user_link(user_id, sender_name)
        recipient_link = get_user_link(recipient_id, recipient_name)
        
        await message.reply_html(
            translate_text(f"✅ Tipped <b>{tip_amount}⭐</b> to {recipient_link}", user_id=user_id)
        )
        
        try:
            await context.bot.send_message(
                chat_id=recipient_id,
                text=translate_text(
                    f"🎂 <b>You received a tip!</b>\n\n"
                    f"👤 From: {sender_link}\n"
                    f"💰 Amount: <b>{tip_amount} ⭐</b> (${tip_usd:.2f})\n\n"
                    f"💵 Your new balance: <b>{get_user_balance(recipient_id)} ⭐</b>"
                ),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Could not notify recipient {recipient_id}: {e}")
        
        logger.info(f"Tip: {user_id} ({sender_name}) -> {recipient_id} ({recipient_name}): {tip_amount} stars")
        
    except ValueError:
        await message.reply_html(translate_text("❌ Invalid amount! Please enter a number.", user_id=user_id))


@handle_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    # Auto-detect user language from Telegram language_code
    user_lang_code = getattr(user, 'language_code', None) or ""

    if user_id not in user_languages:
        detected = detect_lang(user_lang_code)
        user_languages[user_id] = detected
        db.set_user_language(user_id, detected)
        logger.info(f"User {user_id} language detected: {user_lang_code} → {detected}")
    
    # Check if user is banned
    if is_banned(user_id):
        return  # Silently ignore banned users
    
    # Check for start parameters (e.g., /start withdraw, /start deposit, /start ref-CODE)
    if context.args and len(context.args) > 0:
        start_param = context.args[0].lower()
        if start_param == "withdraw":
            # Redirect to withdraw command
            await withdraw_command(update, context)
            return
        elif start_param == "deposit":
            # Redirect to deposit command
            await deposit_command(update, context)
            return
        elif start_param == "support":
            # Redirect to support command
            await support_command(update, context)
            return
        elif start_param.startswith("ref-"):
            # Handle referral code
            try:
                ref_code = start_param.replace("ref-", "").strip()
                if ref_code and ref_code in referral_code_to_user:
                    referrer_id = referral_code_to_user[ref_code]
                    # Only set referrer if user doesn't already have one and isn't referring themselves
                    if user_id not in user_referrers and user_id != referrer_id:
                        user_referrers[user_id] = referrer_id
                        user_referrals[referrer_id].add(user_id)
                        save_data()
                        logger.info(f"User {user_id} joined via referral code {ref_code} from user {referrer_id}")
            except Exception as e:
                logger.error(f"Error processing referral code: {e}", exc_info=True)
    
    get_or_create_profile(user_id, user.username or user.first_name)
    
    # Update username mapping
    if user.username:
        username_to_id[user.username.lower()] = user_id
        save_data()
    
    balance = get_user_balance(user_id)
    balance_usd = balance * STARS_TO_USD
    
    profile = user_profiles.get(user_id, {})
    turnover = profile.get('total_bets', 0.0) * STARS_TO_USD
    
    admin_badge = " 👑" if is_admin(user_id) else ""
    
    # Get bot identity
    bot_name = bot_identity.get("name", "Iibrate")
    channel_link_raw = bot_identity.get("channel_link", "https://t.me/Iibrate")
    chat_link_raw = bot_identity.get("chat_link", "https://t.me/librateds")
    support_username = bot_identity.get("support_username", "Iibratesupport")
    
    # Format channel link (convert @username to https://t.me/username)
    if channel_link_raw.startswith('@'):
        channel_link = f"https://t.me/{channel_link_raw[1:]}"
    elif not channel_link_raw.startswith('http'):
        channel_link = f"https://t.me/{channel_link_raw.replace('@', '')}"
    else:
        channel_link = channel_link_raw
    
    # Format chat link (convert @username to https://t.me/username)
    if chat_link_raw.startswith('@'):
        chat_link = f"https://t.me/{chat_link_raw[1:]}"
    elif not chat_link_raw.startswith('http'):
        chat_link = f"https://t.me/{chat_link_raw.replace('@', '')}"
    else:
        chat_link = chat_link_raw
    
    # Format support link
    if support_username.startswith('@'):
        support_link = f"https://t.me/{support_username[1:]}"
    else:
        support_link = f"https://t.me/{support_username}"
    
    # ── Message 1: Welcome / Getting Started (same template as welcome)
    start_info = t(
        "start_info",
        user_id=user_id,
        bot_name=bot_name,
        admin_badge=admin_badge,
        balance_usd=balance_usd,
        turnover=turnover,
        channel_link=channel_link,
        chat_link=chat_link,
        support_link=support_link,
    )
    await update.message.reply_html(start_info)

    # ── Message 2: Inline Menu ──
    menu_keyboard = [
        [
            InlineKeyboardButton(t("btn_deposit", user_id=user_id), callback_data="balance_deposit"),
            InlineKeyboardButton(t("btn_withdraw", user_id=user_id), callback_data="balance_withdraw"),
        ],
        [
            InlineKeyboardButton(t("btn_balance", user_id=user_id), callback_data="back_to_balance"),
            InlineKeyboardButton(t("btn_stats", user_id=user_id), callback_data="show_profile"),
        ],
        [
            InlineKeyboardButton(t("btn_play", user_id=user_id), callback_data="show_games"),
        ]
    ]
    menu_sent = await update.message.reply_html(
        t("menu_choose", user_id=user_id),
        reply_markup=InlineKeyboardMarkup(menu_keyboard)
    )
    register_menu_owner(menu_sent, user_id)


@handle_errors
async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_or_create_profile(user_id, update.effective_user.username or update.effective_user.first_name)
    
    keyboard = [
        [
            InlineKeyboardButton(t("game_dice", user_id=user_id), callback_data="play_game_dice"),
            InlineKeyboardButton(t("game_bowling", user_id=user_id), callback_data="play_game_bowl"),
        ],
        [
            InlineKeyboardButton(t("game_darts", user_id=user_id), callback_data="play_game_dart"),
            InlineKeyboardButton(t("game_football", user_id=user_id), callback_data="play_game_football"),
        ],
        [
            InlineKeyboardButton(t("game_basketball", user_id=user_id), callback_data="play_game_basket"),
            InlineKeyboardButton(t("game_coinflip", user_id=user_id), callback_data="play_game_coinflip"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    play_text = t("play_text", user_id=user_id)
    sent = await send_bot_reply_html(
        update.message, play_text, message_key="play",
        reply_markup=reply_markup, chat_id=update.effective_chat.id
    )
    register_menu_owner(sent, user_id)


# ==================== CASINO LEVELS SYSTEM ====================

def get_user_level(total_bets_usd):
    """Calculate user's level based on total bets in USD"""
    try:
        # Ensure total_bets_usd is a valid number
        if not isinstance(total_bets_usd, (int, float)):
            total_bets_usd = 0.0
        total_bets_usd = max(0.0, float(total_bets_usd))
        
        level = 0
        for lvl, threshold in sorted(LEVEL_THRESHOLDS.items(), reverse=True):
            if total_bets_usd >= threshold:
                level = lvl
                break
        return max(0, min(25, level))
    except Exception as e:
        logger.error(f"Error in get_user_level: {e}", exc_info=True)
        return 0


def get_level_progress(total_bets_usd, current_level):
    """Calculate progress percentage to next level"""
    try:
        # Ensure inputs are valid
        if not isinstance(total_bets_usd, (int, float)):
            total_bets_usd = 0.0
        total_bets_usd = max(0.0, float(total_bets_usd))
        current_level = int(max(0, min(25, current_level)))
        
        if current_level >= 25:  # MAX LEVEL
            return 100
        
        current_threshold = LEVEL_THRESHOLDS.get(current_level, 0)
        next_threshold = LEVEL_THRESHOLDS.get(current_level + 1)
        
        if next_threshold is None or next_threshold == current_threshold:
            return 100
        
        if next_threshold - current_threshold == 0:
            return 100
        
        progress = ((total_bets_usd - current_threshold) / (next_threshold - current_threshold)) * 100
        return max(0, min(100, progress))
    except Exception as e:
        logger.error(f"Error in get_level_progress: {e}", exc_info=True)
        return 0


def create_progress_bar(percentage, length=20):
    """Create a progress bar with filled and empty blocks"""
    try:
        percentage = float(percentage) if percentage else 0.0
        percentage = max(0, min(100, percentage))
        filled = int((percentage / 100) * length)
        empty = max(0, length - filled)
        return "▰" * filled + "▱" * empty
    except Exception:
        return "▱" * length


def format_level_display(user_id, username=None):
    """Format the level display for a user"""
    profile = get_or_create_profile(user_id, username)
    total_bets = profile.get('total_bets', 0.0)
    total_bets_usd = total_bets * STARS_TO_USD
    
    current_level = get_user_level(total_bets_usd)
    # Ensure level is within valid range
    current_level = max(0, min(25, current_level))
    level_info = CASINO_LEVELS.get(current_level, CASINO_LEVELS[0])
    progress = get_level_progress(total_bets_usd, current_level)
    
    # Current level features
    current_rakeback = level_info.get('rakeback', 5.0)
    current_weekly = level_info.get('weekly_mult', 1.09)
    
    # Next level info
    if current_level < 25:
        next_level = current_level + 1
        next_level_info = CASINO_LEVELS.get(next_level)
        if not next_level_info:
            next_level_info = CASINO_LEVELS[25]  # Fallback to max level
        next_rakeback = next_level_info.get('rakeback', current_rakeback)
        next_weekly = next_level_info.get('weekly_mult', current_weekly)
        level_up_bonus = next_level_info.get('level_up_bonus', 0)
        next_level_name = next_level_info.get('name', 'MAX LEVEL')
    else:
        next_level = None
        next_level_name = "MAX LEVEL"
        next_rakeback = current_rakeback
        next_weekly = current_weekly
        level_up_bonus = 0
    
    progress_bar = create_progress_bar(progress)
    
    text = f"Your profile Level: <b>{level_info.get('name', 'Steel')} (Lvl {current_level})</b>\n"
    text += f"Progress: <b>{progress:.1f}%</b> → {next_level_name}\n"
    text += f"{progress_bar}\n\n"
    
    text += f"<b>[{level_info.get('name', 'Steel')}] features:</b>\n"
    text += f"Rakeback: <b>{current_rakeback}%</b>\n"
    text += f"Weekly bonus: <b>{current_weekly}x</b>\n\n"
    
    if current_level < 25:
        text += f"<b>[{next_level_name}] features:</b>\n"
        text += f"Level-Up bonus: <b>${level_up_bonus}</b>\n"
        text += f"Rakeback: <b>{current_rakeback}%</b> → <b>{next_rakeback}%</b>\n"
        text += f"Weekly bonus: <b>{current_weekly}x</b> → <b>{next_weekly}x</b>\n"
    
    return text


@handle_errors
async def levels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's level and all available levels"""
    try:
        user = update.effective_user
        user_id = user.id
        
        profile = get_or_create_profile(user_id, user.username or user.first_name)
        total_bets = profile.get('total_bets', 0.0)
        
        # Ensure total_bets is a valid number
        try:
            total_bets = float(total_bets) if total_bets else 0.0
        except (ValueError, TypeError):
            total_bets = 0.0
        
        total_bets_usd = total_bets * STARS_TO_USD
        
        # Initialize all variables with defaults
        current_level = 0
        level_info = CASINO_LEVELS[0]
        progress = 0.0
        current_rakeback = 5.0
        current_weekly = 1.09
        next_rakeback = 6.5
        next_weekly = 1.09
        level_up_bonus = 5
        next_level_name = "Iron I"
        level_name = "Steel"
        
        try:
            current_level = get_user_level(total_bets_usd)
            # Ensure level is within valid range
            current_level = max(0, min(25, int(current_level)))
            level_info = CASINO_LEVELS.get(current_level)
            if not level_info:
                level_info = CASINO_LEVELS[0]
            
            progress = get_level_progress(total_bets_usd, current_level)
            if progress is None:
                progress = 0.0
            progress = float(progress)
            
            # Current level features
            current_rakeback = float(level_info.get('rakeback', 5.0))
            current_weekly = float(level_info.get('weekly_mult', 1.09))
            level_name = str(level_info.get('name', 'Steel'))
            
            # Next level info
            if current_level < 25:
                next_level = current_level + 1
                next_level_info = CASINO_LEVELS.get(next_level)
                if next_level_info:
                    next_rakeback = float(next_level_info.get('rakeback', current_rakeback))
                    next_weekly = float(next_level_info.get('weekly_mult', current_weekly))
                    level_up_bonus = int(next_level_info.get('level_up_bonus', 0))
                    next_level_name = str(next_level_info.get('name', 'MAX LEVEL'))
                else:
                    next_level_name = "MAX LEVEL"
                    next_rakeback = current_rakeback
                    next_weekly = current_weekly
                    level_up_bonus = 0
            else:
                next_level_name = "MAX LEVEL"
                next_rakeback = current_rakeback
                next_weekly = current_weekly
                level_up_bonus = 0
        except Exception as e:
            logger.error(f"Error calculating level info: {e}", exc_info=True)
            # Use defaults already set above
        
        try:
            progress_bar = create_progress_bar(progress)
            if not progress_bar:
                progress_bar = "▱" * 20
        except Exception:
            progress_bar = "▱" * 20
        
        # Build the message text
        try:
            text = f"Your profile Level: <b>{level_name} (Lvl {current_level})</b>\n"
            text += f"Progress: <b>{progress:.1f}%</b> → {next_level_name}\n"
            text += f"{progress_bar}\n\n"
            
            text += f"<b>[{level_name}] features:</b>\n"
            text += f"Rakeback: <b>{current_rakeback}%</b>\n"
            text += f"Weekly bonus: <b>{current_weekly}x</b>\n\n"
            
            if current_level < 25:
                text += f"<b>[{next_level_name}] features:</b>\n"
                text += f"Level-Up bonus: <b>${level_up_bonus}</b>\n"
                text += f"Rakeback: <b>{current_rakeback}%</b> → <b>{next_rakeback}%</b>\n"
                text += f"Weekly bonus: <b>{current_weekly}x</b> → <b>{next_weekly}x</b>\n\n"
            
            text += "Use /levels to see all the rank levels, benefits and bonuses"
            
            await update.message.reply_html(text)
        except Exception as e:
            logger.error(f"Error formatting level text: {e}", exc_info=True)
            raise
    except Exception as e:
        logger.error(f"Error in levels_command: {e}", exc_info=True)
        await update.message.reply_html(
            translate_text(
                "❌ <b>An error occurred while displaying your level.</b>\n\n"
                "Please try again later."
            )
        )


@handle_errors
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    profile = get_or_create_profile(user_id, user.username or user.first_name)
    balance = get_user_balance(user_id)
    balance_usd = balance * STARS_TO_USD
    
    user_link = get_user_link(user_id, user.first_name)
    
    # Favorite game (dynamically calculated)
    fav_game = profile.get('favorite_game')
    if fav_game and fav_game in GAME_TYPES:
        fav_game_display = f"{GAME_TYPES[fav_game]['icon']} {GAME_TYPES[fav_game]['name']}"
    elif fav_game and fav_game in GAME_CONFIG:
        fav_game_display = f"{GAME_CONFIG[fav_game]['emoji']} {GAME_CONFIG[fav_game]['name']}"
    else:
        fav_game_display = "None"
    
    # Biggest win
    biggest_win = profile.get('biggest_win', 0)
    biggest_win_usd = biggest_win * STARS_TO_USD if biggest_win > 0 else 0.0
    
    # Registration date (DD.MM.YYYY format)
    reg_date = profile.get('registration_date', datetime.now())
    reg_date_str = reg_date.strftime("%d.%m.%Y")
    
    # Total bets and wins in USD
    try:
        total_bets = float(profile.get('total_bets', 0) or 0)
    except (ValueError, TypeError):
        total_bets = 0.0
    
    try:
        total_wins = float(profile.get('total_wins', 0) or 0)
    except (ValueError, TypeError):
        total_wins = 0.0
    
    total_bets_usd = total_bets * STARS_TO_USD
    total_wins_usd = total_wins * STARS_TO_USD
    
    # Rank from level system
    try:
        current_level = get_user_level(total_bets_usd)
        current_level = max(0, min(25, current_level))
        level_info = CASINO_LEVELS.get(current_level, CASINO_LEVELS[0])
        rank_name = level_info.get('name', 'Steel')
    except Exception:
        rank_name = "Steel"
    
    total_games = profile.get('total_games', 0)
    
    profile_text = (
        f"👤 <b>Profile</b>\n\n"
        f"â¹ï¸  User: {user_link} (<code>{user_id}</code>)\n"
        f"🏅 Rank: {rank_name}\n"
        f"💰 Balance: <b>${balance_usd:.2f}</b>\n\n"
        f"⚡ Total games: <b>{total_games}</b>\n"
        f"Total bet amount: <b>${total_bets_usd:.2f}</b>\n"
        f"Total winnings: <b>${total_wins_usd:.2f}</b>\n\n"
        f"🎲 Favorite game: {fav_game_display}\n"
        f"🎉 Biggest win: <b>${biggest_win_usd:.2f}</b>\n\n"
        f"🕒 Registration date: {reg_date_str}"
    )
    
    await send_bot_reply_html(
        update.message, profile_text, message_key="profile",
        chat_id=update.effective_chat.id
    )


# Old progress bar function removed - using the new one for levels


@handle_errors
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    profile = get_or_create_profile(user_id, user.username or user.first_name)
    history = user_game_history.get(user_id, [])
    
    total_games = profile.get('total_games', 0)
    total_bets = profile.get('total_bets', 0)
    total_wins = profile.get('total_wins', 0)
    total_losses = profile.get('total_losses', 0)
    games_won = profile.get('games_won', 0)
    games_lost = profile.get('games_lost', 0)
    
    total_wagered = total_bets
    net_profit = total_wins - total_losses
    
    total_bets_usd = total_bets * STARS_TO_USD
    total_wins_usd = total_wins * STARS_TO_USD
    total_losses_usd = total_losses * STARS_TO_USD
    total_wagered_usd = total_wagered * STARS_TO_USD
    net_profit_usd = net_profit * STARS_TO_USD
    
    if total_games > 0:
        win_rate = (games_won / total_games) * 100
    else:
        win_rate = 0
    
    history_text = (
        f"📊 <b>Game History</b>\n\n"
        f"🎮 <b>Total Games Played:</b> {total_games}\n"
        f"✅ Games Won: {games_won}\n"
        f"❌ Games Lost: {games_lost}\n"
        f"📈 Win Rate: {win_rate:.1f}%\n\n"
        f"💰 <b>Financial Summary:</b>\n"
        f"💵 Total Bets: ${total_bets_usd:.2f}\n"
        f"💰 Total Wins: ${total_wins_usd:.2f}\n"
        f"📉 Total Losses: ${total_losses_usd:.2f}\n"
        f"🔄 Total Wagered: ${total_wagered_usd:.2f}\n"
        f"{'📈' if net_profit >= 0 else '📉'} Net Profit: ${net_profit_usd:.2f}\n"
    )
    
    if history:
        history_text += "\n📜 <b>Recent Games:</b>\n"
        recent_games = history[-5:]
        for game in reversed(recent_games):
            game_type = game['game_type']
            game_info = GAME_TYPES.get(game_type, {'icon': '🎮', 'name': 'Unknown'})
            status = "✅ Won" if game['won'] else "❌ Lost"
            bet_usd = game['bet_amount'] * STARS_TO_USD
            timestamp = game['timestamp'].strftime("%m/%d %H:%M")
            history_text += f"{game_info['icon']} {game_info['name']} - {status} (${bet_usd:.2f}) - {timestamp}\n"
    
    await update.message.reply_html(history_text)


def format_matches_page(history_list, page, total_pages):
    """Format a single page of match history matching screenshot style."""
    start = page * MATCHES_PER_PAGE
    end = start + MATCHES_PER_PAGE
    page_entries = history_list[start:end]

    if not page_entries:
        return "📋 <b>Game history</b>\n\nNo matches found."

    text = "📋 <b>Game history</b>\n"

    for entry in page_entries:
        game_type = entry.get('game_type', 'unknown')
        display = MATCH_GAME_DISPLAY.get(game_type, {'emoji': '🎮', 'name': game_type.title()})
        emoji = display['emoji']
        name = display['name']

        match_id = entry.get('match_id', 0)

        ts = entry.get('timestamp')
        if isinstance(ts, datetime):
            ts_str = ts.strftime("%d.%m.%Y  %H:%M")
        elif isinstance(ts, str):
            try:
                ts_str = datetime.fromisoformat(ts).strftime("%d.%m.%Y  %H:%M")
            except Exception:
                ts_str = ts
        else:
            ts_str = "—"

        bet_usd = entry.get('bet_amount', 0) * STARS_TO_USD
        win_usd = entry.get('win_amount', 0) * STARS_TO_USD

        text += (
            f"\n{emoji} {name} #{match_id} | {ts_str}\n"
            f"💰 Bet: <b>${bet_usd:.2f}</b>\n"
            f"👑 Win: <b>${win_usd:.2f}</b>\n"
        )

    text += f"\nPage {page + 1}/{total_pages}"
    return text


@handle_errors
async def matches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Paginated game history - /matches"""
    user = update.effective_user
    user_id = user.id

    get_or_create_profile(user_id, user.username or user.first_name)
    history = user_game_history.get(user_id, [])

    if not history:
        await update.message.reply_html(
            "📋 <b>Game history</b>\n\n"
            "No matches yet. Play a game to see your history!"
        )
        return

    # Build reversed list (newest first) with match IDs
    total = len(history)
    history_reversed = []
    for i, entry in enumerate(reversed(history)):
        entry_copy = dict(entry)
        entry_copy['match_id'] = MATCH_ID_BASE + total - i
        history_reversed.append(entry_copy)

    total_pages = max(1, (len(history_reversed) + MATCHES_PER_PAGE - 1) // MATCHES_PER_PAGE)
    page = 0

    text = format_matches_page(history_reversed, page, total_pages)

    # Build pagination buttons
    buttons = []
    if total_pages > 1:
        buttons.append(InlineKeyboardButton("âž¡ï¸¯¸", callback_data=f"matches_page_{page + 1}"))
    keyboard = [buttons] if buttons else []
    keyboard.append([InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="matches_back")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await update.message.reply_html(text, reply_markup=reply_markup)
    register_menu_owner(sent, user_id)


# ══════════════════════════════════════════════════════════════════════════════
# LEADERBOARD — Image generation + inline filter buttons
# ══════════════════════════════════════════════════════════════════════════════

# ── Hardcoded Leaderboard Data ──────────────────────────────────────────
LEADERBOARD_DATA = {
    "wins": {
        "title": "🏆 Most Wins",
        "entries": [
            ("🥇", "@zo_Yuji", "550 wins"),
            ("🥈", "@strut", "358 wins"),
            ("🥉", "?", "349 wins"),
            ("4.", "@sanixhhhhh", "307 wins"),
            ("5.", "@Agentplugz", "258 wins"),
            ("6.", "@Temporarilyuser", "251 wins"),
            ("7.", "@nawaz", "238 wins"),
            ("8.", "@simpstonate", "227 wins"),
        ]
    },
    "money": {
        "title": "💰 Most Money Won",
        "entries": [
            ("🥇", "@bnbsolxrpbtc", "$93,805"),
            ("🥈", "@nine", "$50,060"),
            ("🥉", "@frog", "$47,997"),
            ("4.", "@strut", "$43,394"),
            ("5.", "@OGUfed", "$40,070"),
            ("6.", "@qqqqqqqqqqqqq1237", "$25,529"),
            ("7.", "?", "$24,401"),
            ("8.", "@nawaz", "$19,886"),
        ]
    },
    "active": {
        "title": "🎮 Most Active",
        "entries": [
            ("🥇", "@zo_Yuji", "941 games"),
            ("🥈", "?", "737 games"),
            ("🥉", "@strut", "680 games"),
            ("4.", "@sanixhhhhh", "602 games"),
            ("5.", "@Agentplugz", "496 games"),
            ("6.", "@Temporarilyuser", "468 games"),
            ("7.", "@nawaz", "457 games"),
            ("8.", "@OGUfed", "442 games"),
        ]
    },
    "roller": {
        "title": "🎲 Highest Roller",
        "entries": [
            ("🥇", "@bnbsolxrpbtc", "$95,545"),
            ("🥈", "@nine", "$63,383"),
            ("🥉", "@frog", "$51,276"),
            ("4.", "@OGUfed", "$43,891"),
            ("5.", "@niiigggaaaaa", "$38,687"),
            ("6.", "@qqqqqqqqqqq4237", "$34,210"),
            ("7.", "?", "$27,770"),
            ("8.", "@NoHelm", "$20,490"),
        ]
    },
}

_LB_DIR = os.path.dirname(os.path.abspath(__file__))
LEADERBOARD_IMAGES = {
    "wins": os.path.join(_LB_DIR, "lb_wins.jpg"),
    "money": os.path.join(_LB_DIR, "lb_money.png"),
    "active": os.path.join(_LB_DIR, "lb_active.jpg"),
    "roller": os.path.join(_LB_DIR, "lb_roller.jpg"),
}


def _build_lb_caption(category):
    """Build a formatted leaderboard caption for the given category."""
    data = LEADERBOARD_DATA[category]
    lines = [f"<b>{data['title']}</b>\n"]
    for rank, name, value in data["entries"]:
        lines.append(f"{rank} <b>{name}</b> — {value}")
    return "\n".join(lines)


def _build_lb_keyboard():
    """Build the 2x2 inline keyboard for leaderboard categories."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏆 Most Wins", callback_data="lb_wins"),
            InlineKeyboardButton("💰 Most Money Won", callback_data="lb_money"),
        ],
        [
            InlineKeyboardButton("🎮 Most Active", callback_data="lb_active"),
            InlineKeyboardButton("🎲 Highest Roller", callback_data="lb_roller"),
        ],
    ])


@handle_errors
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show leaderboard with photo and category filter buttons."""
    user = update.effective_user
    get_or_create_profile(user.id, user.username or user.first_name)

    caption = _build_lb_caption("wins")
    markup = _build_lb_keyboard()

    with open(LEADERBOARD_IMAGES["wins"], "rb") as img:
        sent = await update.message.reply_photo(
            photo=img,
            caption=caption,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
    register_menu_owner(sent, user.id)


@handle_errors
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    help_text = t("help_text", bot_username=BOT_USERNAME)
    
    if is_admin(user_id):
        help_text += t("admin_commands")
    
    await update.message.reply_html(help_text)


@handle_errors
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = get_user_balance(user_id)
    balance_usd = balance * STARS_TO_USD
    
    admin_note = " (Admin - Unlimited)" if is_admin(user_id) else ""
    
    # Get bot username for URL buttons
    try:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username if bot_info.username else "Iibratebot"
    except Exception:
        bot_username = "Iibratebot"  # Fallback
    
    # Try to use template first
    template_sent = await send_template_message(
        update.message, context, "balance", user_id,
        admin_note=admin_note,
        balance=balance,
        balance_usd=balance_usd
    )
    
    if template_sent:
        register_menu_owner(template_sent, user_id)
        return
    
    # Fallback to default message
    keyboard = [
        [
            InlineKeyboardButton(t("deposit_button"), url=f"https://t.me/{bot_username}?start=deposit"),
            InlineKeyboardButton(t("withdraw_button"), url=f"https://t.me/{bot_username}?start=withdraw"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    balance_text = t("your_balance", admin_note=admin_note, balance=balance, balance_usd=balance_usd)
    sent = await send_bot_reply_html(
        update.message, balance_text, message_key="balance",
        reply_markup=reply_markup, chat_id=update.effective_chat.id
    )
    register_menu_owner(sent, user_id)


@handle_errors
async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [
        [
            InlineKeyboardButton("10 ⭐", callback_data="deposit_10"),
            InlineKeyboardButton("25 ⭐", callback_data="deposit_25"),
        ],
        [
            InlineKeyboardButton("50 ⭐", callback_data="deposit_50"),
            InlineKeyboardButton("100 ⭐", callback_data="deposit_100"),
        ],
        [
            InlineKeyboardButton("250 ⭐", callback_data="deposit_250"),
            InlineKeyboardButton("500 ⭐", callback_data="deposit_500"),
        ],
        [
            InlineKeyboardButton(t("custom_amount_button", user_id=user_id), callback_data="deposit_custom"),
        ],
        [
            InlineKeyboardButton(t("crypto_deposit_button", user_id=user_id), callback_data="crypto_deposit"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    sent = await send_bot_reply_html(
        update.message, t("select_deposit", user_id=user_id), message_key="deposit",
        reply_markup=reply_markup, chat_id=update.effective_chat.id
    )
    register_menu_owner(sent, update.effective_user.id)


@handle_errors
async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # If command is used in group, show message with redirect button
    if update.effective_chat.type != "private":
        # Get bot username for URL button
        try:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username if bot_info.username else "Iibratebot"
        except Exception:
            bot_username = "Iibratebot"  # Fallback
        
        keyboard = [
            [
                InlineKeyboardButton(t("btn_withdraw", user_id=user_id), url=f"https://t.me/{bot_username}?start=withdraw")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_html(
            t("please_use_private"),
            reply_markup=reply_markup
        )
        return
    
    context.user_data['withdraw_state'] = None
    context.user_data['withdraw_amount'] = None
    context.user_data['withdraw_address'] = None
    context.user_data['withdraw_type'] = None  # 'stars' or 'crypto'
    
    welcome_text = t("welcome_withdraw", min_withdrawal=MIN_WITHDRAWAL)
    
    keyboard = [
        [
            InlineKeyboardButton(t("withdraw_stars_button", user_id=user_id), callback_data="withdraw_stars"),
            InlineKeyboardButton(t("withdraw_crypto_button", user_id=user_id), callback_data="withdraw_crypto"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Send with video if set, otherwise just text
    sent = None
    if withdraw_video_file_id:
        try:
            sent = await update.message.reply_video(
                video=withdraw_video_file_id,
                caption=welcome_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Failed to send withdraw video: {e}")
            # Fallback to text if video fails
            sent = await update.message.reply_html(welcome_text, reply_markup=reply_markup)
    else:
        sent = await update.message.reply_html(welcome_text, reply_markup=reply_markup)
    
    if sent:
        register_menu_owner(sent, user_id)


def create_mines_grid_keyboard(game: MinesGame):
    """Create inline keyboard for mines game grid"""
    keyboard = []
    
    # If game is lost, reveal all mines
    reveal_all = (game.game_state == "lost")
    
    for row in range(game.grid_size):
        row_buttons = []
        for col in range(game.grid_size):
            if reveal_all:
                # Game over - show all mines and opened tiles
                if (row, col) in game.mines_positions:
                    # All mines revealed
                    row_buttons.append(InlineKeyboardButton("💣", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
                elif (row, col) in game.opened_tiles:
                    # Opened safe tile (diamond)
                    row_buttons.append(InlineKeyboardButton("💎", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
                else:
                    # Unopened safe tile
                    row_buttons.append(InlineKeyboardButton("💎", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
            elif (row, col) in game.opened_tiles:
                if (row, col) in game.mines_positions:
                    # Mine revealed (game over)
                    row_buttons.append(InlineKeyboardButton("💣", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
                else:
                    # Diamond found
                    row_buttons.append(InlineKeyboardButton("💎", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
            else:
                # Unopened tile
                row_buttons.append(InlineKeyboardButton("💎", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
        keyboard.append(row_buttons)
    
    # Add cash out button if diamonds found and game is still playing
    if game.diamonds_found > 0 and game.game_state == "playing":
        current_win = game.get_current_win()
        profit = current_win - game.bet_amount
        cash_out_text = t("mines_cash_out", user_id=game.user_id, amount=current_win, profit=profit)
        keyboard.append([InlineKeyboardButton(cash_out_text, callback_data=f"mines_cashout_{game.game_id}")])
    
    return InlineKeyboardMarkup(keyboard)


def format_mines_game_message(game: MinesGame):
    """Format the mines game display message"""
    multiplier = game.calculate_multiplier()
    current_win = game.get_current_win()
    
    profit = current_win - game.bet_amount
    total_tiles = game.grid_size * game.grid_size
    remaining_safe = total_tiles - game.num_mines - game.diamonds_found
    
    message = "💎 <b>MINES</b>\n\n"
    message += f"━━━━━━━━━━━━━━━━━━━━\n"
    message += f"📊 <b>Game Info</b>\n"
    message += f"Grid: <b>{game.grid_size}×{game.grid_size}</b> | Mines: <b>{game.num_mines}</b> 💣\n"
    message += f"💎 Diamonds Found: <b>{game.diamonds_found}</b>\n"
    message += f"🟦 Safe Tiles Remaining: <b>{remaining_safe}</b>\n\n"
    message += f"━━━━━━━━━━━━━━━━━━━━\n"
    message += f"💰 <b>Bet Amount:</b> <b>{game.bet_amount:,} ⭐</b>\n"
    message += f"📈 <b>Current Multiplier:</b> <b>{multiplier}x</b>\n"
    message += f"💵 <b>Potential Win:</b> <b>{current_win:,} ⭐</b>\n"
    if profit > 0:
        message += f"📊 <b>Profit:</b> <b>+{profit:,} ⭐</b>\n"
    message += f"━━━━━━━━━━━━━━━━━━━━"
    
    return message


@handle_errors
async def mines_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mines game command"""
    user_id = update.effective_user.id
    
    # Check if user has active game
    if user_id in mines_games:
        game = mines_games[user_id]
        # Check if game expired (5 minutes)
        if (datetime.now() - game.last_click_time).total_seconds() > 300:
            del mines_games[user_id]
        else:
            # Show current game
            message = format_mines_game_message(game)
            keyboard = create_mines_grid_keyboard(game)
            await update.message.reply_html(message, reply_markup=keyboard)
            return
    
    # Show grid size selection
    keyboard = [
        [
            InlineKeyboardButton("3×3", callback_data="mines_grid_3"),
            InlineKeyboardButton("4×4", callback_data="mines_grid_4"),
            InlineKeyboardButton("5×5", callback_data="mines_grid_5"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    balance = get_user_balance(user_id)
    
    await update.message.reply_html(
        "💎 <b>MINES</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Your Balance:</b> <b>{balance:,} ⭐</b>\n\n"
        "🎯 <b>Select Grid Size:</b>\n\n"
        "• <b>3×3</b> - 9 tiles (1-4 mines)\n"
        "• <b>4×4</b> - 16 tiles (1-7 mines)\n"
        "• <b>5×5</b> - 25 tiles (1-12 mines)\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        reply_markup=reply_markup
    )


def create_mines_grid_keyboard(game: MinesGame):
    """Create inline keyboard for mines game grid"""
    keyboard = []
    
    # If game is lost, reveal all mines
    reveal_all = (game.game_state == "lost")
    
    for row in range(game.grid_size):
        row_buttons = []
        for col in range(game.grid_size):
            if reveal_all:
                # Game over - show all mines and opened tiles
                if (row, col) in game.mines_positions:
                    # All mines revealed
                    row_buttons.append(InlineKeyboardButton("💣", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
                elif (row, col) in game.opened_tiles:
                    # Opened safe tile (diamond)
                    row_buttons.append(InlineKeyboardButton("💎", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
                else:
                    # Unopened safe tile
                    row_buttons.append(InlineKeyboardButton("💎", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
            elif (row, col) in game.opened_tiles:
                if (row, col) in game.mines_positions:
                    # Mine revealed (game over)
                    row_buttons.append(InlineKeyboardButton("💣", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
                else:
                    # Diamond found
                    row_buttons.append(InlineKeyboardButton("💎", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
            else:
                # Unopened tile
                row_buttons.append(InlineKeyboardButton("💎", callback_data=f"mine_click_{row}_{col}_{game.game_id}"))
        keyboard.append(row_buttons)
    
    # Add cash out button if diamonds found and game is still playing
    if game.diamonds_found > 0 and game.game_state == "playing":
        current_win = game.get_current_win()
        profit = current_win - game.bet_amount
        cash_out_text = t("mines_cash_out", user_id=game.user_id, amount=current_win, profit=profit)
        keyboard.append([InlineKeyboardButton(cash_out_text, callback_data=f"mines_cashout_{game.game_id}")])
    
    return InlineKeyboardMarkup(keyboard)


def format_mines_game_message(game: MinesGame):
    """Format the mines game display message"""
    multiplier = game.calculate_multiplier()
    current_win = game.get_current_win()
    
    profit = current_win - game.bet_amount
    total_tiles = game.grid_size * game.grid_size
    remaining_safe = total_tiles - game.num_mines - game.diamonds_found
    
    message = "💎 <b>MINES</b>\n\n"
    message += f"━━━━━━━━━━━━━━━━━━━━\n"
    message += f"📊 <b>Game Info</b>\n"
    message += f"Grid: <b>{game.grid_size}×{game.grid_size}</b> | Mines: <b>{game.num_mines}</b> 💣\n"
    message += f"💎 Diamonds Found: <b>{game.diamonds_found}</b>\n"
    message += f"🟦 Safe Tiles Remaining: <b>{remaining_safe}</b>\n\n"
    message += f"━━━━━━━━━━━━━━━━━━━━\n"
    message += f"💰 <b>Bet Amount:</b> <b>{game.bet_amount:,} ⭐</b>\n"
    message += f"📈 <b>Current Multiplier:</b> <b>{multiplier}x</b>\n"
    message += f"💵 <b>Potential Win:</b> <b>{current_win:,} ⭐</b>\n"
    if profit > 0:
        message += f"📊 <b>Profit:</b> <b>+{profit:,} ⭐</b>\n"
    message += f"━━━━━━━━━━━━━━━━━━━━"
    
    return message


@handle_errors
async def mines_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mines game command"""
    user_id = update.effective_user.id
    
    # Check if user has active game
    if user_id in mines_games:
        game = mines_games[user_id]
        # Check if game expired (5 minutes)
        if (datetime.now() - game.last_click_time).total_seconds() > 300:
            del mines_games[user_id]
        else:
            # Show current game
            message = format_mines_game_message(game)
            keyboard = create_mines_grid_keyboard(game)
            await update.message.reply_html(message, reply_markup=keyboard)
            return
    
    # Show grid size selection
    keyboard = [
        [
            InlineKeyboardButton("3×3", callback_data="mines_grid_3"),
            InlineKeyboardButton("4×4", callback_data="mines_grid_4"),
            InlineKeyboardButton("5×5", callback_data="mines_grid_5"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    balance = get_user_balance(user_id)
    
    await update.message.reply_html(
        "💎 <b>MINES</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Your Balance:</b> <b>{balance:,} ⭐</b>\n\n"
        "🎯 <b>Select Grid Size:</b>\n\n"
        "• <b>3×3</b> - 9 tiles (1-4 mines)\n"
        "• <b>4×4</b> - 16 tiles (1-7 mines)\n"
        "• <b>5×5</b> - 25 tiles (1-12 mines)\n\n"
        "━━━━━━━━━━━━━━━━━━━━",
        reply_markup=reply_markup
    )


@handle_errors
async def custom_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or len(context.args) == 0:
        await update.message.reply_html(
            "💳 <b>Custom Deposit</b>\n\n"
            "Usage: /custom <amount>\n"
            "Example: /custom 150\n\n"
            "Minimum: 1 ⭐\n"
            "Maximum: 10000 ⭐"
        )
        return

    try:
        amount = int(context.args[0])

        if amount < 1:
            await update.message.reply_html(translate_text("❌ Minimum deposit is 1 ⭐", user_id=user_id))
            return

        if amount > 10000:
            await update.message.reply_html(translate_text("❌ Maximum deposit is 10000 ⭐", user_id=user_id))
            return
        
        title = f"Deposit {amount} Stars"
        description = f"Add {amount} ⭐ to your game balance"
        payload = f"deposit_{amount}_{update.effective_user.id}"
        prices = [LabeledPrice("Stars", amount)]
        
        await update.message.reply_invoice(
            title=title,
            description=description,
            payload=payload,
            provider_token=PROVIDER_TOKEN,
            currency="XTR",
            prices=prices
        )
    except ValueError:
        await update.message.reply_html(translate_text("❌ Invalid amount! Please enter a number.", user_id=user_id))


async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE, game_type: str):
    """Core function for starting a new point-based game"""
    user_id = update.effective_user.id
    
    async with game_locks[user_id]:
        if user_id in game_sessions:
            await update.message.reply_html(
                "❌ You already have an active game! Finish it first."
            )
            return
        
        balance = get_user_balance(user_id)
        
        bet_amount = None
        if context.args and len(context.args) > 0:
            arg = context.args[0].lower()
            if arg == 'all':
                bet_amount = int(balance)
            elif arg == 'half':
                bet_amount = int(balance / 2)
            else:
                try:
                    bet_amount = int(arg)
                except ValueError:
                    await update.message.reply_html(translate_text("❌ Invalid bet amount! Use a number, 'all', or 'half'.", user_id=user_id))
                    return
            
            if bet_amount < 1:
                await update.message.reply_html(translate_text("❌ Bet amount must be at least 1 ⭐", user_id=user_id))
                return
            
            if bet_amount > balance and not is_admin(user_id):
                await update.message.reply_html(
                    f"❌ Insufficient balance!\n"
                    f"Your balance: <b>{balance} ⭐</b>\n"
                    f"Bet amount: <b>{bet_amount} ⭐</b>"
                )
                return
            
            # Store bet, go directly to mode selection
            config = GAME_CONFIG[game_type]
            context.user_data['bet_amount'] = bet_amount
            context.user_data['game_type'] = game_type
            
            keyboard = [
                [InlineKeyboardButton(t("mode_normal", user_id=user_id), callback_data=f"mode_normal_{game_type}")],
                [InlineKeyboardButton(t("mode_double", user_id=user_id), callback_data=f"mode_double_{game_type}")],
                [InlineKeyboardButton(t("mode_crazy", user_id=user_id), callback_data=f"mode_crazy_{game_type}")],
                [InlineKeyboardButton(t("cancel_game", user_id=user_id), callback_data=f"cancel_{game_type}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            sent = await update.message.reply_html(
                "🎲 <b>Select game mode</b>\n\n"
                "<i>• Normal mode: Highest value wins\n"
                "• Crazy mode: Lowest value wins\n"
                "• Double mode: 2 emojis are rolled in 1 round</i>",
                reply_markup=reply_markup
            )
            register_menu_owner(sent, user_id)
            return
        
        if balance < 1 and not is_admin(user_id):
            await update.message.reply_html(
                "❌ Insufficient balance! Use /deposit to add Stars.\n"
                f"Your balance: <b>{balance} ⭐</b>"
            )
            return
        
        config = GAME_CONFIG[game_type]
        context.user_data['game_type'] = game_type
        
        keyboard = [
            [
                InlineKeyboardButton("10 ⭐", callback_data=f"bet_{game_type}_10"),
                InlineKeyboardButton("25 ⭐", callback_data=f"bet_{game_type}_25"),
            ],
            [
                InlineKeyboardButton("50 ⭐", callback_data=f"bet_{game_type}_50"),
                InlineKeyboardButton("100 ⭐", callback_data=f"bet_{game_type}_100"),
            ],
            [
                InlineKeyboardButton(t("cancel_game", user_id=user_id), callback_data=f"cancel_{game_type}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        sent = await update.message.reply_html(
            f"{config['emoji']} <b>{config['name']}</b>\n\n"
            f"💰 Choose your bet:\n"
            f"Your balance: <b>{balance:,} ⭐</b>",
            reply_markup=reply_markup
        )
        register_menu_owner(sent, user_id)


@handle_errors
async def dice_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_game(update, context, game_type="dice")


@handle_errors
async def dart_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_game(update, context, game_type="dart")


@handle_errors
async def football_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_game(update, context, game_type="football")


@handle_errors
async def basket_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_game(update, context, game_type="basket")


@handle_errors
async def bowl_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_game(update, context, game_type="bowl")


@handle_errors
async def demo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ This command is only for administrators.", user_id=user_id))
        return
    
    if user_id in game_sessions:
        await update.message.reply_html(
            "❌ You already have an active game! Finish it first."
        )
        return
    
    keyboard = [
        [
            InlineKeyboardButton(t("game_dice", user_id=user_id), callback_data="demo_game_dice"),
            InlineKeyboardButton(t("game_bowl", user_id=user_id), callback_data="demo_game_bowl"),
        ],
        [
            InlineKeyboardButton(t("game_dart", user_id=user_id), callback_data="demo_game_dart"),
            InlineKeyboardButton(t("game_football", user_id=user_id), callback_data="demo_game_football"),
        ],
        [
            InlineKeyboardButton(t("game_basketball", user_id=user_id), callback_data="demo_game_basket"),
        ],
        [
            InlineKeyboardButton(t("cancel_demo", user_id=user_id), callback_data="cancel_demo"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_html(
        f"🎮 <b>DEMO MODE</b> 🔑\n\n"
        f"🎯 Choose a game to test:\n"
        f"(No Stars will be deducted)",
        reply_markup=reply_markup
    )


async def start_round(context, chat_id, user_id):
    """Prepare session for next player roll (user always rolls first)"""
    session = game_sessions.get(user_id)
    if not session:
        return
    mode = session['mode']
    session['player_rolls_needed'] = 2 if mode == "double" else 1
    session['player_rolls_done'] = 0
    session['player_total'] = 0
    session['waiting_for_player'] = True


async def complete_round(context, chat_id, user_id):
    """Compare scores, send round result, continue or end game"""
    session = game_sessions.get(user_id)
    if not session:
        return

    game_type = session['game_type']
    mode = session['mode']

    player_val = session['player_total']
    bot_val = session['bot_value']

    profile = get_or_create_profile(user_id)
    display_name = profile.get('display_name') or profile.get('username') or 'Player'
    user_link = get_user_link(user_id, display_name)
    game_emoji = GAME_CONFIG.get(game_type, {}).get('emoji', '🎲')
    copy_turn_markup = build_copy_turn_reply_markup(user_id, game_emoji)

    # --- TIE ---
    if player_val == bot_val:
        b_score = session['bot_score']
        p_score = session['player_score']
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🤝 It's a tie!\n\n"
                f"Scores:\n"
                f"👤 Bot • {b_score}\n"
                f"👤 {user_link} • {p_score}\n\n"
                f"🎮 Waiting for {display_name}...\n"
                f"👉 Next round: {user_link}, it's your turn."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=copy_turn_markup
        )
        await start_round(context, chat_id, user_id)
        return

    # --- DETERMINE ROUND WINNER ---
    if mode == "crazy":
        player_wins = player_val < bot_val
    else:
        player_wins = player_val > bot_val

    if player_wins:
        session['player_score'] += 1
    else:
        session['bot_score'] += 1

    p_score = session['player_score']
    b_score = session['bot_score']
    target = session['points_target']
    bet = session['bet']
    is_demo = session.get('is_demo', False)
    multiplier = session['multiplier']

    scores_block = (
        f"Scores:\n"
        f"👤 Bot • {b_score}\n"
        f"👤 {user_link} • {p_score}"
    )

    round_header = f"🏆 {display_name} wins this round!" if player_wins else "🏆 Bot wins this round!"
    demo_tag = " 🔑" if is_demo else ""

    # --- GAME OVER ---
    if p_score >= target or b_score >= target:
        bet_usd = bet * STARS_TO_USD
        earned_usd = bet_usd * multiplier

        if p_score >= target:
            winnings_int = int(bet * multiplier)
            if not is_demo:
                paid = adjust_user_balance(user_id, winnings_int, game=True)
                if paid is False:
                    final_line = "🔧 <b>Casino Maintenance</b>\n\nThe casino is temporarily unable to process this win. Please try again shortly."
                else:
                    user_balances[user_id] = get_user_balance(user_id)
                    stats_game_type = 'arrow' if game_type == 'dart' else game_type
                    update_game_stats(user_id, stats_game_type, bet, winnings_int, True)
                    save_last_game_settings(user_id, game_type, bet, mode, target)
                    final_line = f"🎉 {user_link} wins the game and earns ${earned_usd:.2f} {multiplier}x"
            else:
                final_line = f"🎉 {user_link} wins the game and earns ${earned_usd:.2f} {multiplier}x"
        else:
            if not is_demo:
                stats_game_type = 'arrow' if game_type == 'dart' else game_type
                update_game_stats(user_id, stats_game_type, bet, 0, False)
                save_last_game_settings(user_id, game_type, bet, mode, target)
            final_line = "💀 Bot wins the game.\nBetter luck next time!"

        if user_id in game_sessions:
            del game_sessions[user_id]

        game_over_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(t("play_again", user_id=user_id), callback_data=f"replay_{game_type}"),
                InlineKeyboardButton(t("back_to_games", user_id=user_id), callback_data="show_games"),
            ]
        ])

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{round_header}{demo_tag}\n\n"
                f"{scores_block}\n\n"
                f"{final_line}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=game_over_keyboard
        )

    # --- ROUND CONTINUES ---
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{round_header}\n\n"
                f"{scores_block}\n\n"
                f"🎮 Waiting for {display_name}...\n"
                f"👉 Next round: {user_link}, it's your turn."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=copy_turn_markup
        )
        await start_round(context, chat_id, user_id)


async def send_invoice(query, amount):
    title = f"Deposit {amount} Stars"
    description = f"Add {amount} ⭐ to your game balance"
    payload = f"deposit_{amount}_{query.from_user.id}"
    prices = [LabeledPrice("Stars", amount)]

    try:
        await query.message.reply_invoice(
            title=title,
            description=description,
            payload=payload,
            provider_token=PROVIDER_TOKEN,
            currency="XTR",
            prices=prices
        )
        await query.edit_message_text(
            f"💳 Invoice for <b>{amount} ⭐</b> sent!\n"
            f"Complete the payment to add Stars to your balance.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        try:
            await query.answer(
                "ℹ️ Our servers are refreshing this table. Please try again shortly.",
                show_alert=True
            )
        except Exception:
            pass


def format_withdrawal_status(status):
    """Format withdrawal status for display"""
    status_map = {
        'on_hold': 'â ³ Pending',
        'cancelled': '🚫 Cancelled',
        'completed': '✅ Completed',
        'draft': '📝 Draft'
    }
    return status_map.get(status, status)


def format_withdrawal_date(date_str):
    """Format withdrawal date for display"""
    try:
        if isinstance(date_str, str):
            dt = datetime.fromisoformat(date_str)
            return dt.strftime("%d.%m %H:%M")
        return str(date_str)
    except:
        return str(date_str)


@handle_errors
async def handle_support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all support ticket callbacks"""
    global ticket_counter
    query = update.callback_query
    if not query:
        return
    
    user_id = query.from_user.id
    data = query.data
    
    if data == "support_create_ticket":
        # Ask which bot/topic
        keyboard = [
            [
                InlineKeyboardButton(t("support_withdraw_topic", user_id=user_id), callback_data="support_topic_withdraw"),
                InlineKeyboardButton(t("support_other_topic", user_id=user_id), callback_data="support_topic_other")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Which bot do you need help with?",
            reply_markup=reply_markup
        )
        await query.answer()
        return
    
    elif data == "support_my_tickets":
        # Show user's tickets
        user_ticket_list = user_tickets.get(user_id, [])
        if not user_ticket_list:
            await query.edit_message_text(
                "🗒 <b>My Tickets</b>\n\n"
                "You don't have any tickets yet.",
                parse_mode=ParseMode.HTML
            )
            await query.answer()
            return
        
        tickets_text = "🗒 <b>My Tickets</b>\n\n"
        for idx, ticket in enumerate(user_ticket_list[-10:], 1):  # Show last 10 tickets
            ticket_id = ticket.get('ticket_id', 'N/A')
            topic = ticket.get('topic', 'Unknown')
            status = ticket.get('status', 'open')
            created = ticket.get('created', '')
            tickets_text += f"{idx}. Ticket #{ticket_id} - {topic} ({status})\n"
        
        await query.edit_message_text(tickets_text, parse_mode=ParseMode.HTML)
        await query.answer()
        return
    
    elif data == "support_topic_withdraw":
        # Show withdrawal history as inline buttons
        buttons = []
        
        # Get all withdrawals for user
        # user_withdrawals structure: {str(user_id): {withdrawal_data}}
        all_withdrawals = []
        
        # Check if user has a withdrawal stored
        user_withdrawal = user_withdrawals.get(str(user_id))
        if user_withdrawal and isinstance(user_withdrawal, dict) and 'exchange_id' in user_withdrawal:
            all_withdrawals.append(user_withdrawal)
        
        # Also check all withdrawals to find ones for this user
        # (in case structure is different or there are multiple)
        for key, withdrawal in user_withdrawals.items():
            if isinstance(withdrawal, dict) and 'exchange_id' in withdrawal:
                # If key is user_id, it's for that user
                try:
                    if int(key) == user_id:
                        if withdrawal not in all_withdrawals:
                            all_withdrawals.append(withdrawal)
                except:
                    pass
        
        # Sort by date (newest first)
        try:
            all_withdrawals.sort(key=lambda x: x.get('created', ''), reverse=True)
        except:
            pass
        
        # Limit to 20 withdrawals for display
        display_withdrawals = all_withdrawals[:20]
        
        if not display_withdrawals:
            await query.edit_message_text(
                "❌ <b>No withdrawals found.</b>\n\n"
                "You don't have any withdrawal history.",
                parse_mode=ParseMode.HTML
            )
            await query.answer()
            return
        
        # Build text and buttons
        page_num = 1
        withdrawal_text = f"Select the exchange you need help with.\nPage {page_num}.\n\n"
        
        for withdrawal in display_withdrawals:
            exchange_id = withdrawal.get('exchange_id', 'N/A')
            stars = withdrawal.get('stars', 0)
            ton_amount = withdrawal.get('ton_amount', 0)
            status = withdrawal.get('status', 'draft')
            created = withdrawal.get('created', '')
            
            status_display = format_withdrawal_status(status)
            
            # Parse date format: "2024-12-07 06:27" -> "07.12 06:27"
            try:
                if isinstance(created, str):
                    if ' ' in created:
                        date_part, time_part = created.split(' ', 1)
                        year, month, day = date_part.split('-')
                        hour, minute = time_part.split(':')[:2]
                        date_display = f"{day}.{month} {hour}:{minute}"
                    else:
                        date_display = created
                else:
                    date_display = str(created)
            except:
                date_display = str(created)
            
            # Format: Two lines per withdrawal
            # Line 1: "Date — Status · Stars → TON · Date"
            # Line 2: "#ExchangeID — Status · Stars → TON · Date"
            withdrawal_text += f"{date_display} — {status_display} · {stars:,} STARS → {ton_amount:.2f} TON · {date_display}\n#{exchange_id} — {status_display} · {stars:,} STARS → {ton_amount:.2f} TON · {date_display}\n"
            
            # Create button for each withdrawal
            button_text = f"#{exchange_id} - {status_display}"
            if len(button_text) > 64:  # Telegram button text limit
                button_text = f"#{exchange_id}"
            buttons.append([InlineKeyboardButton(button_text, callback_data=f"support_withdraw_{exchange_id}")])
        
        reply_markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(withdrawal_text, reply_markup=reply_markup)
        await query.answer()
        return
    
    elif data.startswith("support_withdraw_"):
        # User selected a withdrawal
        exchange_id = data.replace("support_withdraw_", "")
        
        # Store selected withdrawal in context
        context.user_data['support_selected_withdrawal'] = exchange_id
        
        keyboard = [
            [InlineKeyboardButton(t("support_issue_frozen", user_id=user_id), callback_data="support_issue_frozen")],
            [InlineKeyboardButton(t("support_issue_locked", user_id=user_id), callback_data="support_issue_locked")],
            [InlineKeyboardButton(t("support_issue_not_received", user_id=user_id), callback_data="support_issue_not_received")],
            [InlineKeyboardButton(t("support_issue_other", user_id=user_id), callback_data="support_issue_other")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "👋 Hello! What seems to be the problem?",
            reply_markup=reply_markup
        )
        await query.answer()
        return
    
    elif data in ["support_issue_frozen", "support_issue_locked", "support_issue_other"]:
        # Create ticket and send wait message
        ticket_id = ticket_counter
        ticket_counter = db.get_ticket_counter() + 1
        db.set_ticket_counter(ticket_counter)
        
        issue_type = {
            "support_issue_frozen": "Transaction frozen",
            "support_issue_locked": "Account locked",
            "support_issue_other": "Another question"
        }.get(data, "Unknown issue")
        
        # Create ticket
        if user_id not in user_tickets:
            user_tickets[user_id] = []
        
        ticket = {
            'ticket_id': ticket_id,
            'user_id': user_id,
            'topic': 'Withdraw',
            'issue': issue_type,
            'withdrawal_id': context.user_data.get('support_selected_withdrawal'),
            'status': 'open',
            'created': datetime.now().isoformat()
        }
        
        user_tickets[user_id].append(ticket)  # Keep in memory for compatibility
        db.add_ticket(
            ticket_id=ticket_id,
            user_id=user_id,
            topic=ticket.get('topic'),
            issue=ticket.get('issue'),
            withdrawal_id=ticket.get('withdrawal_id'),
            status=ticket.get('status', 'open'),
            created=datetime.now()
        )
        
        await query.edit_message_text(
            translate_text("⏳ Please wait—our managers will contact you as soon as possible to resolve your issue.", user_id=user_id)
        )
        await query.answer()
        return
    
    elif data == "support_issue_not_received":
        # Ask how they topped up
        keyboard = [
            [
                InlineKeyboardButton(t("support_topup_fragment", user_id=user_id), callback_data="support_topup_fragment"),
                InlineKeyboardButton(t("support_topup_store", user_id=user_id), callback_data="support_topup_store")
            ],
            [
                InlineKeyboardButton(t("support_topup_premium", user_id=user_id), callback_data="support_topup_premium"),
                InlineKeyboardButton(t("support_topup_gifts", user_id=user_id), callback_data="support_topup_gifts")
            ],
            [
                InlineKeyboardButton(t("support_topup_other_bot", user_id=user_id), callback_data="support_topup_other_bot"),
                InlineKeyboardButton(t("support_topup_other", user_id=user_id), callback_data="support_topup_other")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            translate_text("How did you top up stars to your account?", user_id=user_id),
            reply_markup=reply_markup
        )
        await query.answer()
        return
    
    elif data in ["support_topup_fragment", "support_topup_store", "support_topup_premium", 
                  "support_topup_gifts", "support_topup_other_bot", "support_topup_other"]:
        # All buttons (1-6): Ask for screen recording
        logger.info(f"Support topup callback received: {data} from user {user_id}")
        
        ticket_id = ticket_counter
        ticket_counter = db.get_ticket_counter() + 1
        db.set_ticket_counter(ticket_counter)
        
        topup_method = {
            "support_topup_fragment": "Fragment",
            "support_topup_store": "Apple/Google Store",
            "support_topup_premium": "Premium Bot",
            "support_topup_gifts": "Selling Gifts",
            "support_topup_other_bot": "Purchased in another bot",
            "support_topup_other": "Other"
        }.get(data, "Unknown")
        
        # Create ticket
        if user_id not in user_tickets:
            user_tickets[user_id] = []
        
        ticket = {
            'ticket_id': ticket_id,
            'user_id': user_id,
            'topic': 'Withdraw',
            'issue': "Didn't receive TON",
            'topup_method': topup_method,
            'withdrawal_id': context.user_data.get('support_selected_withdrawal'),
            'status': 'open',
            'waiting_for_video': True,  # Flag to track waiting for video
            'created': datetime.now().isoformat()
        }
        
        user_tickets[user_id].append(ticket)  # Keep in memory for compatibility
        db.add_ticket(
            ticket_id=ticket_id,
            user_id=user_id,
            topic=ticket.get('topic'),
            issue=ticket.get('issue'),
            withdrawal_id=ticket.get('withdrawal_id'),
            status=ticket.get('status', 'open'),
            created=datetime.now()
        )
        
        # Store ticket_id in context for video handler
        context.user_data['support_waiting_video_ticket_id'] = ticket_id
        
        # Answer callback and edit message
        try:
            await query.answer()
            await query.edit_message_text(
                translate_text("Please send a screen recording with all your star transactions.", user_id=user_id)
            )
            logger.info(f"Successfully sent screen recording request for ticket {ticket_id}")
        except Exception as e:
            logger.error(f"Error in support topup handler: {e}", exc_info=True)
            # Try to send as new message if edit fails
            try:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="Please send a screen recording with all your star transactions."
                )
            except Exception as e2:
                logger.error(f"Error sending message for support topup: {e2}", exc_info=True)
        return
    
    elif data == "support_topic_other":
        # Handle other topic
        ticket_id = ticket_counter
        ticket_counter = db.get_ticket_counter() + 1
        db.set_ticket_counter(ticket_counter)
        
        # Create ticket
        if user_id not in user_tickets:
            user_tickets[user_id] = []
        
        ticket = {
            'ticket_id': ticket_id,
            'user_id': user_id,
            'topic': 'Other',
            'status': 'open',
            'created': datetime.now().isoformat()
        }
        
        user_tickets[user_id].append(ticket)  # Keep in memory for compatibility
        db.add_ticket(
            ticket_id=ticket_id,
            user_id=user_id,
            topic=ticket.get('topic'),
            issue=ticket.get('issue'),
            withdrawal_id=ticket.get('withdrawal_id'),
            status=ticket.get('status', 'open'),
            created=datetime.now()
        )
        
        await query.edit_message_text(
            translate_text("⏳ Please wait—our managers will contact you as soon as possible to resolve your issue.", user_id=user_id)
        )
        await query.answer()
        return


@handle_errors
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # Auto-detect language on callback if not already set
    if user_id not in user_languages:
        user_lang_code = getattr(query.from_user, 'language_code', None) or ""
        detected = detect_lang(user_lang_code)
        user_languages[user_id] = detected
        db.set_user_language(user_id, detected)

    # Check if user is banned (allow admins)
    if is_banned(user_id) and not is_admin(user_id):
        await query.answer()
        return  # Silently ignore banned users

    # Check if user is frozen (block deposit, withdraw, game callbacks)
    if is_frozen(user_id) and not is_admin(user_id):
        frozen_prefixes = (
            'deposit_', 'withdraw_', 'crypto_deposit', 'play_game_',
            'game_', 'bet_', 'mines_', 'pred_', 'cflip_', 'bj_',
        )
        if any(data.startswith(p) for p in frozen_prefixes):
            await query.answer(t("err_frozen", user_id=user_id), show_alert=True)
            return

    # Callback ownership protection
    key = (query.message.chat_id, query.message.message_id)
    owner_id = menu_owners.get(key)
    if owner_id and owner_id != user_id:
        await query.answer(t("err_not_your_menu", user_id=user_id), show_alert=True)
        return
    
    try:
        # Handle language selection callbacks
        if data.startswith("set_lang_"):
            new_lang = data.replace("set_lang_", "")
            if new_lang in SUPPORTED_LANGS:
                user_languages[user_id] = new_lang
                db.set_user_language(user_id, new_lang)
                lang_names = {"en": "English", "ru": "Ð ÑÑÑÐºÐ¸Ð¹", "de": "Deutsch", "fr": "Français", "zh": "中文"}
                lang_name = lang_names.get(new_lang, new_lang)
                await query.answer(f"✅ {lang_name}", show_alert=False)
                await query.edit_message_text(
                    f"✅ <b>Language changed to {lang_name}!</b>",
                    parse_mode=ParseMode.HTML
                )
            else:
                await query.answer(t("err_unsupported_lang", user_id=user_id), show_alert=True)
            return

        # Handle predict game callbacks
        if data.startswith("pred_"):
            await handle_predict_callback(update, context)
            return

        # Handle steal command callbacks
        if data.startswith("steal_"):
            await query.answer()
            await handle_steal_callback(update, context)
            return

        # Handle bot network callbacks
        if data.startswith("network_"):
            await query.answer()
            if data == "network_sync_confirm":
                bot_info = context.user_data.pop("sync_target_bot", None)
                if not bot_info:
                    await query.edit_message_text(t("sync_expired", user_id=user_id))
                    return
                source_path = os.path.abspath(db.path)
                target_path = bot_info["db_path"]
                try:
                    synced = sync_settings_to_bot(source_path, target_path)
                    details = "\n".join(f"  • {k}: {v}" for k, v in synced.items())
                    await query.edit_message_text(
                        f"✅ <b>Sync completed to {bot_info['name']}!</b>\n\n"
                        f"<b>Synced:</b>\n{details}",
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ Sync failed: {e}")
            elif data == "network_sync_cancel":
                context.user_data.pop("sync_target_bot", None)
                await query.edit_message_text(t("sync_cancelled", user_id=user_id))
            return

        # Handle leaderboard category switches
        if data.startswith("lb_"):
            cat_key = data.replace("lb_", "")
            if cat_key in LEADERBOARD_DATA:
                await query.answer()
                caption = _build_lb_caption(cat_key)
                markup = _build_lb_keyboard()
                try:
                    with open(LEADERBOARD_IMAGES[cat_key], "rb") as img:
                        media = InputMediaPhoto(media=img, caption=caption, parse_mode=ParseMode.HTML)
                        await query.edit_message_media(media=media, reply_markup=markup)
                except Exception:
                    pass
                return

        # Handle support ticket callbacks
        if data.startswith("support_"):
            logger.info(f"Routing support callback: {data} to handle_support_callback")
            await handle_support_callback(update, context)
            return

        # Handle blackjack callbacks (before generic query.answer)
        if data.startswith("bj_"):
            await handle_blackjack_callback(update, context)
            return

        # Handle weekly bonus claim (before generic answer so we can use show_alert)
        if data == "claim_weekly_bonus":
            now = datetime.now()
            iso_year, iso_week, _ = now.isocalendar()
            current_iso_week = (iso_year, iso_week)
            is_bonus_day = (now.weekday() == 5)

            if not is_bonus_day:
                await query.answer(t("bonus_saturday_only", user_id=user_id), show_alert=True)
                return

            bonus_data = user_weekly_bonus_data.get(user_id)
            if not bonus_data or tuple(bonus_data.get("iso_week", ())) != current_iso_week:
                await query.answer(t("err_no_bonus", user_id=user_id), show_alert=True)
                return

            if bonus_data.get("claimed", False):
                await query.answer(t("err_bonus_claimed", user_id=user_id), show_alert=True)
                return

            bonus_usd = bonus_data["amount_usd"]

            # +10% if bot name is in user's profile name
            has_name_bonus = check_bot_name_in_profile(query.from_user)
            if has_name_bonus:
                bonus_usd = round(bonus_usd * 1.10, 2)

            # Convert USD to stars
            bonus_stars = max(1, int(bonus_usd / STARS_TO_USD))

            # Credit balance
            adjust_user_balance(user_id, bonus_stars)
            user_balances[user_id] = get_user_balance(user_id)

            # Mark claimed
            user_weekly_bonus_data[user_id]["claimed"] = True
            user_weekly_bonus_data[user_id]["amount_usd"] = bonus_usd
            user_weekly_bonus_claimed[user_id] = now
            db.set_weekly_bonus_claimed(user_id, now)

            bot_name = bot_identity.get("name", BOT_USERNAME)

            # Edit message to locked state (same amount, locked UI)
            locked_text = (
                f"🎂 <b>Receive a bonus every Saturday</b>\n\n"
                f"<i>If you don't claim it during Saturday — it expires</i>\n"
                f"🔒 <b>Your weekly bonus is locked</b>\n\n"
                f"<blockquote>Add @{bot_name} to your name and get an extra +10% bonus</blockquote>\n\n"
                f"💰 Your bonus: <b>${bonus_usd:.2f}</b>"
            )

            keyboard = [[InlineKeyboardButton(t("claim_bonus_locked", user_id=user_id), callback_data="claim_weekly_bonus_locked")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(locked_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

            name_tag = " (+10% name bonus!)" if has_name_bonus else ""
            await query.answer(f"✅ Claimed ${bonus_usd:.2f} ({bonus_stars} ⭐){name_tag}", show_alert=True)

            logger.info(f"Weekly bonus claimed: user {user_id} got ${bonus_usd:.2f} ({bonus_stars} ⭐) name_bonus={has_name_bonus}")
            return

        if data == "claim_weekly_bonus_locked":
            await query.answer(t("bonus_locked", user_id=user_id), show_alert=True)
            return
        
        # Handle matches pagination
        if data.startswith("matches_page_"):
            page = int(data.replace("matches_page_", ""))
            history = user_game_history.get(user_id, [])

            if not history:
                await query.answer(t("err_no_match_history", user_id=user_id), show_alert=True)
                return

            total = len(history)
            history_reversed = []
            for i, entry in enumerate(reversed(history)):
                entry_copy = dict(entry)
                entry_copy['match_id'] = MATCH_ID_BASE + total - i
                history_reversed.append(entry_copy)

            total_pages = max(1, (len(history_reversed) + MATCHES_PER_PAGE - 1) // MATCHES_PER_PAGE)
            page = max(0, min(page, total_pages - 1))

            text = format_matches_page(history_reversed, page, total_pages)

            buttons = []
            if page > 0:
                buttons.append(InlineKeyboardButton("¢¬â¦¯¸", callback_data=f"matches_page_{page - 1}"))
            if page < total_pages - 1:
                buttons.append(InlineKeyboardButton("âž¡ï¸¯¸", callback_data=f"matches_page_{page + 1}"))
            keyboard = [buttons] if buttons else []
            keyboard.append([InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="matches_back")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            await query.answer()
            return

        if data == "matches_back":
            await query.edit_message_text(t("history_closed", user_id=user_id), parse_mode=ParseMode.HTML)
            await query.answer()
            return
        
        # Answer callback for other handlers
        await query.answer()
        
        # Old game_repeat/game_double removed - new system uses inline flow
        
        # Handle weekly bonus redemption
        if data == "redeem_weekly_bonus":
            user = query.from_user
            
            # Check if it's Saturday
            if not is_saturday():
                await query.edit_message_text(
                    "❌ <b>No bonus available</b>",
                    parse_mode=ParseMode.HTML
                )
                return
            
            # Check if user has already claimed this Saturday
            last_claim = user_weekly_bonus_claimed.get(user_id)
            if last_claim:
                now = datetime.now()
                # Check if last claim was on a Saturday and it's the same date (same Saturday)
                if last_claim.weekday() == 5 and last_claim.date() == now.date():
                    await query.answer(t("err_bonus_claimed_today", user_id=user_id), show_alert=True)
                    return
                # If last claim was on a Saturday but different date, allow (it's a new Saturday)
            
            # Check if user has bot name in profile
            bot_name = bot_identity.get("name", BOT_USERNAME)
            if not check_bot_name_in_profile(user):
                await query.answer(
                    f"❌ Add @{bot_name} to your profile name to claim the weekly bonus!",
                    show_alert=True
                )
                return
            
            # Give random weekly bonus
            weekly_bonus = get_weekly_bonus_amount()
            adjust_user_balance(user_id, weekly_bonus)
            claim_date = datetime.now()
            user_weekly_bonus_claimed[user_id] = claim_date  # Keep in memory for compatibility
            db.set_weekly_bonus_claimed(user_id, claim_date)
            
            balance = get_user_balance(user_id)
            balance_usd = balance * STARS_TO_USD
            
            await query.edit_message_text(
                f"🎂 <b>Weekly Bonus Claimed Successfully!</b>\n\n"
                f"✅ We found <b>@{bot_name}</b> in your profile name!\n\n"
                f"💰 You received: <b>{weekly_bonus} ⭐</b>\n"
                f"💵 New Balance: <b>{balance:,} ⭐</b> (${balance_usd:.2f})\n\n"
                f"🎉 Thank you for supporting us!\n\n"
                f"¢° Next weekly bonus available next Saturday!",
                parse_mode=ParseMode.HTML
            )
            
            logger.info(f"Weekly bonus claimed by user {user_id} ({user.first_name})")
            return
        
        # Handle balance inline buttons
        if data == "balance_deposit":
            keyboard = [
                [
                    InlineKeyboardButton("10 ⭐", callback_data="deposit_10"),
                    InlineKeyboardButton("25 ⭐", callback_data="deposit_25"),
                ],
                [
                    InlineKeyboardButton("50 ⭐", callback_data="deposit_50"),
                    InlineKeyboardButton("100 ⭐", callback_data="deposit_100"),
                ],
                [
                    InlineKeyboardButton("250 ⭐", callback_data="deposit_250"),
                    InlineKeyboardButton("500 ⭐", callback_data="deposit_500"),
                ],
                [
                    InlineKeyboardButton(t("custom_amount_button", user_id=user_id), callback_data="deposit_custom"),
                ],
                [
                    InlineKeyboardButton(t("crypto_deposit_button", user_id=user_id), callback_data="crypto_deposit"),
                ],
                [
                    InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="back_to_balance"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            sent_dep = await query.edit_message_text(
                "💳 <b>Select deposit amount:</b>",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            register_menu_owner(sent_dep, user_id)
            return
        
        if data == "balance_withdraw":
            if query.message.chat.type != "private":
                bot_info = await context.bot.get_me()
                await query.edit_message_text(
                    "🔒 <b>Private Command Only</b>\n\n"
                    "For your security, withdrawals can only be done in a private chat with the bot.\n\n"
                    f"👉 <a href='https://t.me/{bot_info.username}?start=withdraw'>Click here to open DM</a>\n\n"
                    "Then use /withdraw command.",
                    parse_mode=ParseMode.HTML
                )
                return
            
            context.user_data['withdraw_state'] = None
            context.user_data['withdraw_amount'] = None
            context.user_data['withdraw_address'] = None
            
            welcome_text = (
                "✅ <b>Welcome to Stars Withdrawal!</b>\n\n"
                "<b>Withdraw:</b>\n"
                "1 ⭐ = $0.0179 = 0.01201014 TON\n\n"
                f"<b>Minimum withdrawal: {MIN_WITHDRAWAL} ⭐</b>\n\n"
                "<blockquote>â¹ï¸  <b>Good to know:</b>\n"
                "• When you exchange stars through a channel or bot, Telegram keeps a 15% fee and applies a 21-day hold.\n"
                "• We send TON immediately—factoring in this fee and a small service premium.</blockquote>"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton(t("withdraw_stars_button", user_id=user_id), callback_data="withdraw_stars"),
                    InlineKeyboardButton(t("withdraw_crypto_button", user_id=user_id), callback_data="withdraw_crypto"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # For callback, we need to handle video differently
            # If video is set, delete current message and send new one with video
            if withdraw_video_file_id:
                try:
                    await query.message.delete()
                    sent_msg = await context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=withdraw_video_file_id,
                        caption=welcome_text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
                    register_menu_owner(sent_msg, user_id)
                except Exception as e:
                    logger.error(f"Failed to send withdraw video in callback: {e}")
                    sent_edit = await query.edit_message_text(
                        welcome_text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML
                    )
                    register_menu_owner(sent_edit, user_id)
            else:
                sent_edit = await query.edit_message_text(
                    welcome_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
                register_menu_owner(sent_edit, user_id)
            return
        
        # Handle addbal callbacks
        if data.startswith("addbal_stars_"):
            try:
                # Format: addbal_stars_USERID_AMOUNT (amount may have DOT instead of .)
                parts = data.split("_", 3)  # Split into max 4 parts
                if len(parts) >= 4:
                    target_user_id = int(parts[2])
                    amount_str = parts[3].replace('DOT', '.')  # Replace DOT back to .
                    amount = float(amount_str)
                    
                    # Add stars balance (use db directly to bypass admin guard)
                    db.adjust_user_balance(target_user_id, amount)
                    new_balance = db.get_user_balance(target_user_id)
                    user_balances[target_user_id] = new_balance  # Sync memory cache
                    
                    await query.edit_message_text(
                        f"✅ <b>Balance Added Successfully!</b>\n\n"
                        f"👤 User ID: <code>{target_user_id}</code>\n"
                        f"⭐ Added: <b>{amount:,.2f} Stars</b>\n"
                        f"💰 New Balance: <b>{new_balance:,.2f} Stars</b>",
                        parse_mode=ParseMode.HTML
                    )
                    logger.info(f"Admin {user_id} added {amount} stars to user {target_user_id}")
                else:
                    await query.answer(t("err_invalid_data", user_id=user_id), show_alert=True)
            except (ValueError, IndexError) as e:
                await query.answer(t("err_processing", user_id=user_id), show_alert=True)
                logger.error(f"Error in addbal_stars callback: {e}")
            return
        
        if data.startswith("addbal_crypto_"):
            try:
                # Format: addbal_crypto_USERID_AMOUNT (amount may have DOT instead of .)
                parts = data.split("_", 3)  # Split into max 4 parts
                if len(parts) >= 4:
                    target_user_id = int(parts[2])
                    amount_str = parts[3].replace('DOT', '.')  # Replace DOT back to .
                    amount = float(amount_str)
                    
                    # Add crypto balance
                    db.adjust_user_crypto_balance(target_user_id, amount)
                    user_crypto_balances[target_user_id] = db.get_user_crypto_balance(target_user_id)
                    
                    new_crypto_balance = user_crypto_balances[target_user_id]
                    
                    await query.edit_message_text(
                        f"✅ <b>Crypto Balance Added Successfully!</b>\n\n"
                        f"👤 User ID: <code>{target_user_id}</code>\n"
                        f"💎 Added: <b>${amount:,.2f}</b>\n"
                        f"💰 New Crypto Balance: <b>${new_crypto_balance:,.2f}</b>",
                        parse_mode=ParseMode.HTML
                    )
                    logger.info(f"Admin {user_id} added ${amount} crypto to user {target_user_id}")
                else:
                    await query.answer(t("err_invalid_data", user_id=user_id), show_alert=True)
            except (ValueError, IndexError) as e:
                await query.answer(t("err_processing", user_id=user_id), show_alert=True)
                logger.error(f"Error in addbal_crypto callback: {e}")
            return
        
        # Mines game handlers
        if data.startswith("mines_grid_"):
            grid_size = int(data.replace("mines_grid_", ""))
            
            # Define available mines based on grid size
            if grid_size == 3:
                mines_options = [1, 2, 3, 4]
            elif grid_size == 4:
                mines_options = [1, 3, 5, 7]
            else:  # 5x5
                mines_options = [1, 3, 5, 7, 10, 12]
            
            keyboard = []
            row = []
            for mines in mines_options:
                total_tiles = grid_size * grid_size
                safe_tiles = total_tiles - mines
                max_multiplier = round((total_tiles / safe_tiles) ** safe_tiles, 2)
                row.append(InlineKeyboardButton(f"{mines} 💣", callback_data=f"mines_mines_{grid_size}_{mines}"))
                if len(row) == 2:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💎 <b>MINES</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>Grid Size:</b> <b>{grid_size}×{grid_size}</b>\n"
                f"🎯 <b>Total Tiles:</b> <b>{grid_size * grid_size}</b>\n\n"
                f"💣 <b>Select Number of Mines:</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            return
        
        if data.startswith("mines_mines_"):
            parts = data.split("_")
            grid_size = int(parts[2])
            num_mines = int(parts[3])
            
            # Store in context and ask for bet amount
            context.user_data['mines_grid_size'] = grid_size
            context.user_data['mines_num_mines'] = num_mines
            context.user_data['waiting_for_mines_bet'] = True
            
            total_tiles = grid_size * grid_size
            safe_tiles = total_tiles - num_mines
            max_multiplier = round((total_tiles / safe_tiles) ** safe_tiles, 2)
            balance = get_user_balance(user_id)
            
            await query.edit_message_text(
                f"💎 <b>MINES</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>Game Configuration</b>\n"
                f"Grid: <b>{grid_size}×{grid_size}</b>\n"
                f"Mines: <b>{num_mines}</b> 💣\n"
                f"Safe Tiles: <b>{safe_tiles}</b>\n"
                f"Max Multiplier: <b>{max_multiplier}x</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Your Balance:</b> <b>{balance:,} ⭐</b>\n\n"
                f"💫 <b>Enter your bet amount:</b>\n\n"
                f"Example: <code>100</code> or <code>500</code>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )
            return
        
        if data.startswith("mine_click_"):
            # Format: mine_click_row_col_game_id
            parts = data.split("_")
            if len(parts) >= 5:
                row = int(parts[2])
                col = int(parts[3])
                game_id = "_".join(parts[4:])
                
                # Find game by game_id
                game = None
                for uid, g in mines_games.items():
                    if g.game_id == game_id:
                        if uid == user_id:  # Verify ownership
                            game = g
                            break
                
                if not game:
                    await query.answer(t("err_game_expired", user_id=user_id), show_alert=True)
                    return
                
                if game.game_state != "playing":
                    await query.answer(t("err_game_ended", user_id=user_id), show_alert=True)
                    return
                
                # Check cooldown (1 second)
                time_since_last = (datetime.now() - game.last_click_time).total_seconds()
                if time_since_last < 1:
                    await query.answer(t("alert_please_wait", user_id=user_id), show_alert=True)
                    return
                
                # Click tile
                result = game.click_tile(row, col)
                
                if result is None:
                    await query.answer(t("err_tile_opened", user_id=user_id), show_alert=True)
                    return
                
                if result is False:
                    # Hit a mine - game over
                    game.game_state = "lost"
                    
                    # Update game stats (loss) - bet already deducted at start
                    update_game_stats(user_id, "mines", game.bet_amount, 0, False)
                    save_data()
                    
                    # Format professional loss message
                    multiplier = game.calculate_multiplier()
                    loss_message = "💥 <b>GAME OVER</b> 💥\n\n"
                    loss_message += "━━━━━━━━━━━━━━━━━━━━\n"
                    loss_message += f"📊 <b>Game Summary</b>\n"
                    loss_message += f"Grid: <b>{game.grid_size}×{game.grid_size}</b> | Mines: <b>{game.num_mines}</b> 💣\n"
                    loss_message += f"💎 Diamonds Found: <b>{game.diamonds_found}</b>\n"
                    loss_message += f"📈 Final Multiplier: <b>{multiplier}x</b>\n\n"
                    loss_message += "━━━━━━━━━━━━━━━━━━━━\n"
                    loss_message += f"💰 <b>Bet Amount:</b> <b>{game.bet_amount:,} ⭐</b>\n"
                    loss_message += f"❌ <b>Result:</b> <b>-{game.bet_amount:,} ⭐</b>\n\n"
                    loss_message += "💣 <b>You hit a bomb!</b>\n"
                    loss_message += "━━━━━━━━━━━━━━━━━━━━\n\n"
                    
                    # Create grid keyboard with all mines revealed
                    keyboard = create_mines_grid_keyboard(game)
                    keyboard.inline_keyboard.append([InlineKeyboardButton(t("mines_newgame", user_id=user_id), callback_data="mines_newgame")])
                    
                    await query.edit_message_text(loss_message, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                    del mines_games[user_id]
                    return
                
                # Found diamond - update display
                message = format_mines_game_message(game)
                keyboard = create_mines_grid_keyboard(game)
                await query.edit_message_text(message, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                multiplier = game.calculate_multiplier()
                await query.answer(f"💎 Diamond found! {multiplier}x", show_alert=False)
            return
        
        if data.startswith("mines_cashout_"):
            game_id = data.replace("mines_cashout_", "")
            
            # Find game
            game = None
            for uid, g in mines_games.items():
                if g.game_id == game_id:
                    if uid == user_id:
                        game = g
                        break
            
            if not game:
                await query.answer(t("err_game_not_found", user_id=user_id), show_alert=True)
                return
            
            if game.game_state != "playing":
                await query.answer(t("err_game_ended", user_id=user_id), show_alert=True)
                return
            
            # Cash out
            win_amount = game.cash_out()
            profit = win_amount - game.bet_amount
            multiplier = game.calculate_multiplier()
            
            # Add winnings (bet was already deducted at start)
            if not is_admin(user_id):
                paid = adjust_user_balance(user_id, win_amount, game=True)
                if paid is False:
                    await query.answer("🔧 Casino Maintenance — unable to process win. Try again shortly.", show_alert=True)
                    return
                user_balances[user_id] = get_user_balance(user_id)  # Sync memory cache

            # Update game stats
            update_game_stats(user_id, "mines", game.bet_amount, win_amount, True)
            
            # Format professional win message
            win_message = "✅ <b>CASHED OUT!</b> ✅\n\n"
            win_message += "━━━━━━━━━━━━━━━━━━━━\n"
            win_message += f"📊 <b>Game Summary</b>\n"
            win_message += f"Grid: <b>{game.grid_size}×{game.grid_size}</b> | Mines: <b>{game.num_mines}</b> 💣\n"
            win_message += f"💎 Diamonds Found: <b>{game.diamonds_found}</b>\n"
            win_message += f"📈 Final Multiplier: <b>{multiplier}x</b>\n\n"
            win_message += "━━━━━━━━━━━━━━━━━━━━\n"
            win_message += f"💰 <b>Bet Amount:</b> <b>{game.bet_amount:,} ⭐</b>\n"
            win_message += f"💵 <b>Win Amount:</b> <b>{win_amount:,} ⭐</b>\n"
            win_message += f"📊 <b>Profit:</b> <b>+{profit:,} ⭐</b>\n\n"
            win_message += "🎉 <b>Congratulations!</b>\n"
            win_message += "━━━━━━━━━━━━━━━━━━━━\n\n"
            
            # Create final grid display - show opened diamonds and unopened tiles
            grid_text = "<b>Final Grid:</b>\n"
            for r in range(game.grid_size):
                for c in range(game.grid_size):
                    if (r, c) in game.opened_tiles:
                        grid_text += "💎 "
                    else:
                        grid_text += "⬜ "
                grid_text += "\n"
            
            win_message += grid_text
            
            keyboard = [[InlineKeyboardButton(t("mines_newgame", user_id=user_id), callback_data="mines_newgame")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(win_message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            del mines_games[user_id]
            return
        
        if data == "mines_newgame":
            # Reset to grid selection
            keyboard = [
                [
                    InlineKeyboardButton("3×3", callback_data="mines_grid_3"),
                    InlineKeyboardButton("4×4", callback_data="mines_grid_4"),
                    InlineKeyboardButton("5×5", callback_data="mines_grid_5"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            balance = get_user_balance(user_id)
            
            await query.edit_message_text(
                "💎 <b>MINES</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Your Balance:</b> <b>{balance:,} ⭐</b>\n\n"
                "🎯 <b>Select Grid Size:</b>\n\n"
                "• <b>3×3</b> - 9 tiles (1-4 mines)\n"
                "• <b>4×4</b> - 16 tiles (1-7 mines)\n"
                "• <b>5×5</b> - 25 tiles (1-12 mines)\n\n"
                "━━━━━━━━━━━━━━━━━━━━",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            return
        
        if data == "back_to_menu":
            menu_kb = [
                [
                    InlineKeyboardButton(t("btn_deposit", user_id=user_id), callback_data="balance_deposit"),
                    InlineKeyboardButton(t("btn_withdraw", user_id=user_id), callback_data="balance_withdraw"),
                ],
                [
                    InlineKeyboardButton(t("btn_balance", user_id=user_id), callback_data="back_to_balance"),
                    InlineKeyboardButton(t("btn_stats", user_id=user_id), callback_data="show_profile"),
                ],
                [
                    InlineKeyboardButton(t("btn_play", user_id=user_id), callback_data="show_games"),
                ]
            ]
            sent_menu = await query.edit_message_text(
                "🎮 <b>Menu</b>\nChoose the action:",
                reply_markup=InlineKeyboardMarkup(menu_kb),
                parse_mode=ParseMode.HTML
            )
            register_menu_owner(sent_menu, user_id)
            return

        if data == "back_to_balance":
            balance = get_user_balance(user_id)
            balance_usd = balance * STARS_TO_USD
            admin_note = " (Admin - Unlimited)" if is_admin(user_id) else ""

            keyboard = [
                [
                    InlineKeyboardButton(t("btn_deposit_inline", user_id=user_id), callback_data="balance_deposit"),
                    InlineKeyboardButton(t("btn_withdraw_inline", user_id=user_id), callback_data="balance_withdraw"),
                ],
                [
                    InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="back_to_menu"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            sent_balance = await query.edit_message_text(
                f"💰 <b>Your Balance</b>{admin_note}\n\n"
                f"⭐ Stars: <b>{balance:,} ⭐</b>\n"
                f"💵 USD: <b>${balance_usd:.2f}</b>",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            register_menu_owner(sent_balance, user_id)
            return

        if data == "show_profile":
            user = query.from_user
            profile = get_or_create_profile(user_id, user.username or user.first_name)
            balance = get_user_balance(user_id)
            balance_usd = balance * STARS_TO_USD
            total_bets = float(profile.get('total_bets', 0) or 0)
            total_wins = float(profile.get('total_wins', 0) or 0)
            total_bets_usd = total_bets * STARS_TO_USD
            total_wins_usd = total_wins * STARS_TO_USD
            total_games = profile.get('total_games', 0)
            try:
                current_level = get_user_level(total_bets_usd)
                current_level = max(0, min(25, current_level))
                level_info = CASINO_LEVELS.get(current_level, CASINO_LEVELS[0])
                rank_name = level_info.get('name', 'Steel')
            except Exception:
                rank_name = "Steel"
            fav_game = profile.get('favorite_game')
            if fav_game and fav_game in GAME_TYPES:
                fav_game_display = f"{GAME_TYPES[fav_game]['icon']} {GAME_TYPES[fav_game]['name']}"
            elif fav_game and fav_game in GAME_CONFIG:
                fav_game_display = f"{GAME_CONFIG[fav_game]['emoji']} {GAME_CONFIG[fav_game]['name']}"
            else:
                fav_game_display = "None"
            biggest_win = profile.get('biggest_win', 0)
            biggest_win_usd = biggest_win * STARS_TO_USD if biggest_win > 0 else 0.0

            stats_kb = [[InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="back_to_menu")]]
            stats_text = (
                f"📊 <b>Your Stats</b>\n\n"
                f"🏅 Rank: {rank_name}\n"
                f"💰 Balance: <b>${balance_usd:.2f}</b>\n\n"
                f"⚡ Total games: <b>{total_games}</b>\n"
                f"💵 Total wagered: <b>${total_bets_usd:.2f}</b>\n"
                f"💸 Total winnings: <b>${total_wins_usd:.2f}</b>\n"
                f"🏆 Biggest win: <b>${biggest_win_usd:.2f}</b>\n"
                f"🎮 Favorite game: {fav_game_display}"
            )
            await query.edit_message_text(
                stats_text, reply_markup=InlineKeyboardMarkup(stats_kb),
                parse_mode=ParseMode.HTML
            )
            return

        if data == "show_games":
            keyboard = [
                [
                    InlineKeyboardButton(t("game_dice", user_id=user_id), callback_data="play_game_dice"),
                    InlineKeyboardButton(t("game_bowling", user_id=user_id), callback_data="play_game_bowl"),
                ],
                [
                    InlineKeyboardButton(t("game_darts", user_id=user_id), callback_data="play_game_dart"),
                    InlineKeyboardButton(t("game_football", user_id=user_id), callback_data="play_game_football"),
                ],
                [
                    InlineKeyboardButton(t("game_basketball", user_id=user_id), callback_data="play_game_basket"),
                    InlineKeyboardButton(t("game_coinflip", user_id=user_id), callback_data="play_game_coinflip"),
                ],
                [
                    InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="back_to_menu"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            sent_show = await query.edit_message_text(
                "🎮 <b>Select a game to play:</b>\n\n"
                "🎲 <b>Dice</b> - Roll the dice and beat the bot!\n"
                "🎳 <b>Bowling</b> - Strike your way to victory!\n"
                "🎯 <b>Darts</b> - Aim for the bullseye!\n"
                "⚽ <b>Football</b> - Score goals and win!\n"
                "🏀 <b>Basketball</b> - Shoot hoops for stars!\n"
                "🪙 <b>Coinflip</b> - Call it and flip! (/cf amount)",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            register_menu_owner(sent_show, user_id)
            return
        
        if data == "play_game_coinflip":
            await query.edit_message_text(
                "🎲 <b>Coinflip</b>\n\n"
                "Use /cf <amount> to play!\n\n"
                "Examples:\n"
                "• /cf 100 — Bet 100 ⭐\n"
                "• /cf all — Bet entire balance\n"
                "• /cf half — Bet half balance",
                parse_mode=ParseMode.HTML
            )
            return
        
        if data.startswith("play_game_"):
            game_type = data.replace("play_game_", "")
            if game_type not in GAME_CONFIG:
                await query.answer(t("err_unknown_game", user_id=user_id), show_alert=True)
                return
            
            if user_id in game_sessions:
                await query.edit_message_text(
                    "❌ You already have an active game! Finish it first.",
                    parse_mode=ParseMode.HTML
                )
                return
            
            balance = get_user_balance(user_id)
            if balance < 1 and not is_admin(user_id):
                await query.edit_message_text(
                    "❌ Insufficient balance! Use /deposit to add Stars.\n"
                    f"Your balance: <b>{balance} ⭐</b>",
                    parse_mode=ParseMode.HTML
                )
                return
            
            config = GAME_CONFIG[game_type]
            context.user_data['game_type'] = game_type
            
            keyboard = [
                [
                    InlineKeyboardButton("10 ⭐", callback_data=f"bet_{game_type}_10"),
                    InlineKeyboardButton("25 ⭐", callback_data=f"bet_{game_type}_25"),
                ],
                [
                    InlineKeyboardButton("50 ⭐", callback_data=f"bet_{game_type}_50"),
                    InlineKeyboardButton("100 ⭐", callback_data=f"bet_{game_type}_100"),
                ],
                [
                    InlineKeyboardButton(t("back_to_games", user_id=user_id), callback_data="show_games"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            sent_pg = await query.edit_message_text(
                f"{config['emoji']} <b>{config['name']}</b>\n\n"
                f"💰 Choose your bet:\n"
                f"Your balance: <b>{balance:,} ⭐</b>",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            register_menu_owner(sent_pg, user_id)
            return
        
        if data == "withdraw_stars":
            context.user_data['withdraw_state'] = 'waiting_amount'
            context.user_data['withdraw_type'] = 'stars'
            
            # Try to edit caption if it's a video message, otherwise edit text
            try:
                await query.edit_message_caption(
                    caption=f"💫 <b>Enter the number of ⭐ to withdraw:</b>\n\n"
                            f"Minimum: {MIN_WITHDRAWAL} ⭐\n"
                            f"Example: 100",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                try:
                    await query.edit_message_text(
                        f"💫 <b>Enter the number of ⭐ to withdraw:</b>\n\n"
                        f"Minimum: {MIN_WITHDRAWAL} ⭐\n"
                        f"Example: 100",
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logger.error(f"Failed to edit message for withdraw: {e}")
                    try:
                        await query.answer(t("err_occurred", user_id=user_id), show_alert=True)
                    except:
                        pass
            return
        
        if data == "withdraw_crypto":
            logger.info(f"withdraw_crypto callback received from user {user_id}")
            context.user_data['withdraw_state'] = 'waiting_amount'
            context.user_data['withdraw_type'] = 'crypto'
            
            min_crypto_usd = 5.0
            crypto_balance = user_crypto_balances.get(user_id, 0.0)
            
            # Try to edit caption if it's a video message, otherwise edit text
            try:
                await query.edit_message_caption(
                    caption=f"💫 <b>Enter the number to withdraw:</b>\n\n"
                            f"💎 Your Crypto Balance: <b>${crypto_balance:.2f}</b>\n"
                            f"Minimum: ${min_crypto_usd:.0f}\n"
                            f"Example: 10",
                    parse_mode=ParseMode.HTML
                )
                logger.info(f"Successfully edited caption for crypto withdraw")
            except Exception as e1:
                logger.info(f"Failed to edit caption, trying edit_message_text: {e1}")
                try:
                    await query.edit_message_text(
                        f"💫 <b>Enter the number to withdraw:</b>\n\n"
                        f"💎 Your Crypto Balance: <b>${crypto_balance:.2f}</b>\n"
                        f"Minimum: ${min_crypto_usd:.0f}\n"
                        f"Example: 10",
                        parse_mode=ParseMode.HTML
                    )
                    logger.info(f"Successfully edited message for crypto withdraw")
                except Exception as e2:
                    logger.error(f"Failed to edit message for crypto withdraw: {e2}", exc_info=True)
                    try:
                        await query.answer(t("err_occurred", user_id=user_id), show_alert=True)
                    except:
                        pass
            return
        
        if data == "confirm_withdraw":
            global withdrawal_counter
            
            withdraw_type = context.user_data.get('withdraw_type', 'stars')
            crypto_address = context.user_data.get('withdraw_address', '')
            
            withdrawal_counter = db.get_withdrawal_counter() + 1
            db.set_withdrawal_counter(withdrawal_counter)
            exchange_id = withdrawal_counter
            transaction_id = generate_transaction_id()
            now = datetime.now()
            created_date = now.strftime("%Y-%m-%d %H:%M")
            hold_until = (now + timedelta(days=14)).strftime("%Y-%m-%d %H:%M")
            
            if withdraw_type == 'crypto':
                # Crypto withdrawal: check crypto balance and deduct
                amount_usd = context.user_data.get('withdraw_amount_usd', 0)
                
                # Check crypto balance
                crypto_balance = user_crypto_balances.get(user_id, 0.0)
                if amount_usd > crypto_balance:
                    await query.edit_message_text(
                        "❌ <b>Insufficient crypto balance!</b>\n\n"
                        f"Your crypto balance: ${crypto_balance:.2f}\n"
                        f"Requested: ${amount_usd:.2f}\n\n"
                        "Use /withdraw to try again.",
                        parse_mode=ParseMode.HTML
                    )
                    context.user_data['withdraw_state'] = None
                    return
                
                # Deduct from crypto balance (not stars)
                if not is_admin(user_id):
                    db.adjust_user_crypto_balance(user_id, -amount_usd)
                    user_crypto_balances[user_id] = db.get_user_crypto_balance(user_id)
                
                # Get coin name (use detected coin or detect from address)
                coin_name = context.user_data.get('detected_coin') or detect_coin_from_address(crypto_address)
                
                # Generate a more realistic TXID based on coin type
                import secrets
                if coin_name in ["Ethereum", "USDT"]:
                    # Ethereum-style TXID (66 chars: 0x + 64 hex)
                    txid = "0x" + secrets.token_hex(32)
                elif coin_name == "Bitcoin":
                    # Bitcoin-style TXID (64 hex chars)
                    txid = secrets.token_hex(32)
                elif coin_name == "Litecoin":
                    # Litecoin-style TXID (64 hex chars)
                    txid = secrets.token_hex(32)
                elif coin_name == "Solana":
                    # Solana-style TXID (base58, 88 chars)
                    base58_chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
                    txid = ''.join(secrets.choice(base58_chars) for _ in range(88))
                elif coin_name == "TON":
                    # TON-style transaction hash
                    txid = secrets.token_hex(32).upper()
                else:
                    # Default: use generated transaction_id
                    txid = transaction_id
                
                withdrawal_data = {
                    'exchange_id': exchange_id,
                    'type': 'crypto',
                    'amount_usd': amount_usd,
                    'address': crypto_address,
                    'transaction_id': transaction_id,
                    'txid': txid,
                    'coin_name': coin_name,
                    'created': created_date,
                    'hold_until': hold_until,
                    'status': 'on_hold'
                }
                user_withdrawals[str(user_id)] = withdrawal_data  # Keep in memory for compatibility
                
                # Save to database
                db.add_withdrawal(
                    tx_id=str(exchange_id),
                    user_id=user_id,
                    stars=0.0,  # Crypto withdrawals don't use stars
                    ton_amount=None,
                    status='on_hold',
                    exchange_id=str(exchange_id),
                    created=now,
                    data=withdrawal_data
                )
                
                # Send professional confirmation message
                final_message = (
                    f"🚀 <b>Withdrawal Sent Successfully!</b>\n\n"
                    f"Your funds are now being processed on the <b>{coin_name}</b> blockchain.\n\n"
                    f"📊 <b>Transaction Details:</b>\n"
                    f"💎 Amount: <b>${amount_usd:.2f}</b>\n"
                    f"🧾 TXID: <code>{txid}</code>\n"
                    f"⏳ Expected confirmation: <b>5 minutes</b>\n\n"
                    f"✅ Once the transaction is confirmed on the blockchain, the balance will reflect in your wallet.\n\n"
                    f"💡 <i>You can track your transaction using the TXID above.</i>"
                )
                
                await query.edit_message_text(
                    final_message,
                    parse_mode=ParseMode.HTML
                )
            else:
                # Stars withdrawal: check balance and deduct
                stars_amount = context.user_data.get('withdraw_amount', 0)
                ton_address = crypto_address
                
                balance = get_user_balance(user_id)
                if balance < stars_amount:
                    await query.edit_message_text(
                        "❌ <b>Insufficient balance!</b>\n\n"
                        f"Your balance: {balance} ⭐\n"
                        f"Requested: {stars_amount} ⭐\n\n"
                        "Use /withdraw to try again.",
                        parse_mode=ParseMode.HTML
                    )
                    context.user_data['withdraw_state'] = None
                    return
                
                if not is_admin(user_id):
                    adjust_user_balance(user_id, -stars_amount)
                    user_balances[user_id] = get_user_balance(user_id)  # Sync memory cache
                
                ton_amount = round(stars_amount * STARS_TO_TON, 8)
                
                user_withdrawals[str(user_id)] = {
                    'exchange_id': exchange_id,
                    'type': 'stars',
                    'stars': stars_amount,
                    'ton_amount': ton_amount,
                    'address': ton_address,
                    'transaction_id': transaction_id,
                    'created': created_date,
                    'hold_until': hold_until,
                    'status': 'on_hold'
                }
                
                save_data()
                
                receipt_text = (
                    f"📄 <b>Stars withdraw exchange #{exchange_id}</b>\n\n"
                    f"📊 Exchange status: Processing\n"
                    f"⭐ Stars withdrawal: {stars_amount}\n"
                    f"💎 TON amount: {ton_amount}\n\n"
                    f"<b>Sale:</b>\n"
                    f"🎯 Top-up status: Paid\n"
                    f"🏅 Created: {created_date}\n"
                    f"🏦 TON address: <code>{ton_address}</code>\n"
                    f"🧾 Transaction ID: <code>{transaction_id}</code>\n\n"
                    f"💸 Withdrawal status: On hold\n"
                    f"💎 TON amount: {ton_amount}\n"
                    f"🏅 Withdrawal created: {created_date}\n"
                    f"⏳ On hold until: {hold_until}\n"
                    f"📍 Reason: {bot_identity.get('name', 'Iibrate')} game rating is negative. Placed on 14-day hold."
                )
                
                # Send receipt message for stars withdrawal
                await query.edit_message_text(
                    receipt_text,
                    parse_mode=ParseMode.HTML
                )
            
            context.user_data['withdraw_state'] = None
            context.user_data['withdraw_amount'] = None
            context.user_data['withdraw_amount_usd'] = None
            context.user_data['withdraw_address'] = None
            context.user_data['withdraw_type'] = None
            return
        
        if data == "cancel_withdraw":
            context.user_data['withdraw_state'] = None
            context.user_data['withdraw_amount'] = None
            context.user_data['withdraw_amount_usd'] = None
            context.user_data['withdraw_address'] = None
            context.user_data['withdraw_type'] = None
            await query.edit_message_text(
                "❌ <b>Withdrawal cancelled.</b>\n\n"
                "Use /withdraw to start again.",
                parse_mode=ParseMode.HTML
            )
            return
        
        if data.startswith("deposit_"):
            if data == "deposit_custom":
                await query.edit_message_text(
                    "💳 <b>Custom Deposit</b>\n\n"
                    "Please send the amount you want to deposit.\n\n"
                    "Example: Just type <code>150</code>\n\n"
                    "Minimum: 1 ⭐\n"
                    "Maximum: 10000 ⭐",
                    parse_mode=ParseMode.HTML
                )
                context.user_data['waiting_for_custom_amount'] = True
                return
            
            amount = int(data.split("_")[1])
            await send_invoice(query, amount)
            return
        
        # Crypto deposit handlers
        if data == "crypto_deposit":
            keyboard = [
                [
                    InlineKeyboardButton(
                        "💎 OxaPay Invoice (BTC/ETH/USDT/LTC/DOGE)",
                        callback_data="oxapay_deposit"
                    ),
                ],
                [
                    InlineKeyboardButton(t("crypto_litecoin", user_id=user_id), callback_data="crypto_litecoin"),
                    InlineKeyboardButton(t("crypto_bitcoin", user_id=user_id), callback_data="crypto_bitcoin"),
                ],
                [
                    InlineKeyboardButton(t("crypto_ethereum", user_id=user_id), callback_data="crypto_ethereum"),
                    InlineKeyboardButton(t("crypto_solana", user_id=user_id), callback_data="crypto_solana"),
                ],
                [
                    InlineKeyboardButton(t("crypto_ton", user_id=user_id), callback_data="crypto_ton"),
                    InlineKeyboardButton(t("crypto_usdt_bep20", user_id=user_id), callback_data="crypto_usdt_bep20"),
                ],
                [
                    InlineKeyboardButton(t("crypto_usdc_erc20", user_id=user_id), callback_data="crypto_usdc_erc20"),
                    InlineKeyboardButton(t("crypto_monero", user_id=user_id), callback_data="crypto_monero"),
                ],
                [
                    InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="back_to_deposit"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                "💎 <b>Select Cryptocurrency</b>\n\n"
                "Choose a cryptocurrency to deposit:",
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            return
        
        if data == "back_to_deposit":
            keyboard = [
                [
                    InlineKeyboardButton("10 ⭐", callback_data="deposit_10"),
                    InlineKeyboardButton("25 ⭐", callback_data="deposit_25"),
                ],
                [
                    InlineKeyboardButton("50 ⭐", callback_data="deposit_50"),
                    InlineKeyboardButton("100 ⭐", callback_data="deposit_100"),
                ],
                [
                    InlineKeyboardButton("250 ⭐", callback_data="deposit_250"),
                    InlineKeyboardButton("500 ⭐", callback_data="deposit_500"),
                ],
                [
                    InlineKeyboardButton(t("custom_amount_button", user_id=user_id), callback_data="deposit_custom"),
                ],
                [
                    InlineKeyboardButton(t("crypto_deposit_button", user_id=user_id), callback_data="crypto_deposit"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                t("select_deposit", user_id=user_id),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            return
        
        if data.startswith("crypto_"):
            coin_key = data.replace("crypto_", "")
            coin_info = {
                "litecoin": {"name": "Litecoin", "short": "LTC", "emoji": "💳", "network": ""},
                "bitcoin": {"name": "Bitcoin", "short": "BTC", "emoji": "💳", "network": ""},
                "ethereum": {"name": "Ethereum", "short": "ETH", "emoji": "💳", "network": "ERC-20"},
                "solana": {"name": "Solana", "short": "SOL", "emoji": "💳", "network": ""},
                "ton": {"name": "TON", "short": "TON", "emoji": "💳", "network": ""},
                "usdt_bep20": {"name": "USDT", "short": "USDT", "emoji": "💳", "network": "BEP-20"},
                "usdc_erc20": {"name": "USDC", "short": "USDC", "emoji": "💳", "network": "ERC-20"},
                "monero": {"name": "Monero", "short": "XMR", "emoji": "💳", "network": ""},
            }
            
            if coin_key not in coin_info:
                await query.answer(t("err_invalid_coin", user_id=user_id), show_alert=True)
                return
            
            coin_data = coin_info[coin_key]
            coin_name = coin_data["name"]
            coin_short = coin_data["short"]
            coin_emoji = coin_data["emoji"]
            network = coin_data["network"]
            
            # Get base address from crypto_addresses
            if coin_key not in crypto_addresses or not crypto_addresses[coin_key].get("address"):
                await query.answer(t("err_addr_not_set", user_id=user_id), show_alert=True)
                return
            
            address_data = crypto_addresses[coin_key]
            address = address_data.get("address", "")
            address_network = address_data.get("network", network)
            
            # Check if in private chat (DM) - use new format with timer
            if query.message.chat.type == "private":
                # DM: Use temporary address with timer and refresh
                base_address = address
                temp_address, expires_at = get_or_create_temp_address(user_id, coin_key, base_address)
                timer_text = format_timer(expires_at)
                
                # Format message with timer
                message = f"{coin_emoji} <b>{coin_name} deposit</b>\n"
                message += f"To top up your balance, transfer the desired amount to this {coin_short} address.\n\n"
                message += f"<b>Please note:</b>\n"
                message += f"1. The deposit address is temporary and is only issued for 1 hour. A new one will be created after that.\n"
                message += f"2. One address accepts only one payment.\n\n"
                message += f"<b>{coin_short} address:</b>\n<code>{temp_address}</code>\n\n"
                message += f"<b>Expires in:</b> {timer_text}"
                
                keyboard = [
                    [
                        InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="crypto_deposit"),
                        InlineKeyboardButton(t("refresh_button", user_id=user_id), callback_data=f"crypto_refresh_{coin_key}"),
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
            else:
                # Group: Use old format (simple address, no timer)
                message = f"{coin_emoji} <b>{coin_name} deposit</b>\n\n"
                message += f"To top up your balance, transfer the desired amount to this {coin_name} address.\n\n"
                message += f"<b>{coin_name} address:</b>\n<code>{address}</code>\n\n"
                
                if address_network:
                    message += f"<b>Network:</b> {address_network}\n"
                
                message += f"<b>Network fee:</b> 1%"
                
                keyboard = [
                    [
                        InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="crypto_deposit"),
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                message,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            return
        
        # Handle refresh button (DM only)
        if data.startswith("crypto_refresh_"):
            coin_key = data.replace("crypto_refresh_", "")
            
            # Check if in private chat (DM) - refresh only works in DM
            if query.message.chat.type != "private":
                await query.answer(t("err_refresh_dm_only", user_id=user_id), show_alert=True)
                return
            
            coin_info = {
                "litecoin": {"name": "Litecoin", "short": "LTC", "emoji": "💳", "network": ""},
                "bitcoin": {"name": "Bitcoin", "short": "BTC", "emoji": "💳", "network": ""},
                "ethereum": {"name": "Ethereum", "short": "ETH", "emoji": "💳", "network": "ERC-20"},
                "solana": {"name": "Solana", "short": "SOL", "emoji": "💳", "network": ""},
                "ton": {"name": "TON", "short": "TON", "emoji": "💳", "network": ""},
                "usdt_bep20": {"name": "USDT", "short": "USDT", "emoji": "💳", "network": "BEP-20"},
                "usdc_erc20": {"name": "USDC", "short": "USDC", "emoji": "💳", "network": "ERC-20"},
                "monero": {"name": "Monero", "short": "XMR", "emoji": "💳", "network": ""},
            }
            
            if coin_key not in coin_info:
                await query.answer(t("err_invalid_coin", user_id=user_id), show_alert=True)
                return
            
            # Get base address
            if coin_key not in crypto_addresses or not crypto_addresses[coin_key].get("address"):
                await query.answer(t("err_addr_not_set", user_id=user_id), show_alert=True)
                return
            
            base_address = crypto_addresses[coin_key].get("address", "")
            
            # Delete old temp address and create new one
            key = (user_id, coin_key)
            if key in user_temp_crypto_addresses:
                del user_temp_crypto_addresses[key]
            
            # Create new temp address
            temp_address, expires_at = get_or_create_temp_address(user_id, coin_key, base_address)
            timer_text = format_timer(expires_at)
            
            coin_data = coin_info[coin_key]
            coin_name = coin_data["name"]
            coin_short = coin_data["short"]
            coin_emoji = coin_data["emoji"]
            
            # Format message
            message = f"{coin_emoji} <b>{coin_name} deposit</b>\n"
            message += f"To top up your balance, transfer the desired amount to this {coin_short} address.\n\n"
            message += f"<b>Please note:</b>\n"
            message += f"1. The deposit address is temporary and is only issued for 1 hour. A new one will be created after that.\n"
            message += f"2. One address accepts only one payment.\n\n"
            message += f"<b>{coin_short} address:</b>\n<code>{temp_address}</code>\n\n"
            message += f"<b>Expires in:</b> {timer_text}"
            
            keyboard = [
                [
                    InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="crypto_deposit"),
                    InlineKeyboardButton(t("refresh_button", user_id=user_id), callback_data=f"crypto_refresh_{coin_key}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                message,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            await query.answer(t("alert_addr_refreshed", user_id=user_id))
            return

        # ── OxaPay Invoice Deposit ────────────────────────────────────────────

        if data == "oxapay_deposit":
            keyboard = [
                [InlineKeyboardButton(t("oxapay_usdt", user_id=user_id), callback_data="oxapay_cur_USDT")],
                [
                    InlineKeyboardButton(t("oxapay_btc", user_id=user_id),  callback_data="oxapay_cur_BTC"),
                    InlineKeyboardButton(t("oxapay_eth", user_id=user_id),  callback_data="oxapay_cur_ETH"),
                ],
                [
                    InlineKeyboardButton(t("oxapay_ltc", user_id=user_id),   callback_data="oxapay_cur_LTC"),
                    InlineKeyboardButton(t("oxapay_doge", user_id=user_id), callback_data="oxapay_cur_DOGE"),
                ],
                [InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="crypto_deposit")],
            ]
            await query.edit_message_text(
                "💎 <b>OxaPay Crypto Deposit</b>\n\n"
                "Select the cryptocurrency you want to deposit.\n"
                "An invoice with a unique payment address will be generated for you.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        if data.startswith("oxapay_cur_"):
            currency = data[len("oxapay_cur_"):]
            cur_info = oxapay.SUPPORTED_CURRENCIES.get(currency)
            if not cur_info:
                await query.answer(t("err_unsupported_currency", user_id=user_id), show_alert=True)
                return
            keyboard = [
                [
                    InlineKeyboardButton("$5",   callback_data=f"oxapay_inv_{currency}_5"),
                    InlineKeyboardButton("$10",  callback_data=f"oxapay_inv_{currency}_10"),
                    InlineKeyboardButton("$25",  callback_data=f"oxapay_inv_{currency}_25"),
                ],
                [
                    InlineKeyboardButton("$50",  callback_data=f"oxapay_inv_{currency}_50"),
                    InlineKeyboardButton("$100", callback_data=f"oxapay_inv_{currency}_100"),
                ],
                [InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="oxapay_deposit")],
            ]
            await query.edit_message_text(
                f"💎 <b>OxaPay — {cur_info['emoji']} {cur_info['name']} Deposit</b>\n\n"
                f"Network: <b>{cur_info['network']}</b>\n\n"
                f"Select the amount in USD to deposit:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        if data.startswith("oxapay_inv_"):
            # Callback format: oxapay_inv_{CURRENCY}_{USD_AMOUNT}
            # e.g. oxapay_inv_USDT_25 or oxapay_inv_BTC_10
            parts = data.split("_", 3)   # ['oxapay', 'inv', 'CURRENCY', 'AMOUNT']
            if len(parts) < 4:
                await query.answer(t("err_invalid_selection", user_id=user_id), show_alert=True)
                return
            currency = parts[2]
            try:
                usd_amount = float(parts[3])
            except ValueError:
                await query.answer(t("err_invalid_amount_alert", user_id=user_id), show_alert=True)
                return

            cur_info = oxapay.SUPPORTED_CURRENCIES.get(currency)
            if not cur_info:
                await query.answer(t("err_unsupported_currency", user_id=user_id), show_alert=True)
                return

            await query.answer(t("alert_generating_invoice", user_id=user_id))
            await query.edit_message_text(
                "⏳ <b>Creating your deposit invoice…</b>\n\nPlease wait a moment.",
                parse_mode=ParseMode.HTML,
            )

            # Convert USD amount to the target crypto amount
            crypto_amount = await oxapay.get_crypto_amount_for_usd(usd_amount, currency)
            if crypto_amount is None:
                logger.error(
                    f"OxaPay: could not resolve crypto amount for "
                    f"${usd_amount} in {currency} (user {user_id})"
                )
                await query.edit_message_text(
                    "❌ <b>Could not fetch exchange rate.</b>\n\n"
                    "Please try again in a moment or choose a different currency.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="oxapay_deposit")]]
                    ),
                )
                return

            # Create the OxaPay invoice
            response = await oxapay.create_invoice(
                amount=crypto_amount,
                currency=currency,
                user_id=user_id,
            )

            if response is None or response.get("result") != 100:
                result_code = response.get("result") if response else "N/A"
                result_msg  = response.get("message", "") if response else ""
                logger.error(
                    f"OxaPay invoice creation failed for user {user_id}: "
                    f"result={result_code} msg={result_msg}"
                )
                await query.edit_message_text(
                    "❌ <b>Failed to create deposit invoice.</b>\n\n"
                    "Please try again later or contact support.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="oxapay_deposit")]]
                    ),
                )
                return

            track_id     = response.get("trackId", "")
            pay_link     = response.get("payLink", "")
            inv_amount   = response.get("amount", crypto_amount)
            inv_currency = currency  # creation response doesn't echo currency; use what we sent

            # Try to get the static deposit address for this currency so we can
            # display it directly in the bot.  Falls back gracefully if unavailable.
            cur_info        = oxapay.SUPPORTED_CURRENCIES.get(currency, {})
            network_str     = cur_info.get("network", "")
            static_resp     = await oxapay.request_static_address(inv_currency, network_str)
            deposit_address = ""
            if static_resp and static_resp.get("result") == 100:
                deposit_address = static_resp.get("address", "")

            # Persist to DB
            db.create_deposit(
                user_id=user_id,
                track_id=track_id,
                address=deposit_address or pay_link,
                currency=inv_currency,
                amount_usd=usd_amount,
            )
            logger.info(
                f"[DEPOSIT] Invoice saved: user={user_id} trackId={track_id} "
                f"currency={inv_currency} crypto_amount={inv_amount} usd=${usd_amount} "
                f"address={'YES' if deposit_address else 'NO (payLink fallback)'}"
            )

            stars_estimate = int(usd_amount / STARS_TO_USD)

            if deposit_address:
                network_label = f" ({network_str})" if network_str else ""
                deposit_msg = (
                    f"💳 <b>OxaPay Deposit Invoice</b>\n\n"
                    f"💰 Amount: <b>{inv_amount} {inv_currency}</b>  (≈ ${usd_amount:.2f})\n"
                    f"⭐ You will receive: <b>~{stars_estimate:,} Stars</b>\n\n"
                    f"📋 <b>Send exactly {inv_amount} {inv_currency}{network_label} to:</b>\n"
                    f"<code>{deposit_address}</code>\n\n"
                    f"🔖 Track ID: <code>{track_id}</code>\n"
                    f"⏰ Expires in: <b>30 minutes</b>\n\n"
                    f"✅ Your balance will be credited automatically once confirmed.\n"
                    f"⚠️ Send <b>only {inv_currency}</b> to this address — "
                    f"other coins will be lost."
                )
                keyboard = [
                    [InlineKeyboardButton(t("btn_open_payment", user_id=user_id), url=pay_link)],
                    [InlineKeyboardButton(t("crypto_deposit_button", user_id=user_id), callback_data="oxapay_deposit")],
                ]
            else:
                # Static address unavailable — fall back to payment link only
                deposit_msg = (
                    f"💳 <b>OxaPay Deposit Invoice</b>\n\n"
                    f"💰 Amount: <b>{inv_amount} {inv_currency}</b>  (≈ ${usd_amount:.2f})\n"
                    f"⭐ You will receive: <b>~{stars_estimate:,} Stars</b>\n\n"
                    f"👇 <b>Tap <u>Pay Now</u> to see your deposit address and QR code</b>\n\n"
                    f"🔖 Track ID: <code>{track_id}</code>\n"
                    f"⏰ Expires in: <b>30 minutes</b>\n\n"
                    f"✅ Your balance will be credited automatically once confirmed."
                )
                keyboard = [
                    [InlineKeyboardButton(t("btn_pay_now", user_id=user_id), url=pay_link)],
                    [InlineKeyboardButton(t("crypto_deposit_button", user_id=user_id), callback_data="oxapay_deposit")],
                ]

            await query.edit_message_text(
                deposit_msg,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        # ── End OxaPay ────────────────────────────────────────────────────────

        if data.startswith("demo_game_"):
            if not is_admin(user_id):
                await query.answer(t("err_admin_only_alert", user_id=user_id), show_alert=True)
                return
            
            game_type = data.replace("demo_game_", "")
            if game_type not in GAME_CONFIG:
                await query.answer(t("err_unknown_game", user_id=user_id), show_alert=True)
                return
            
            context.user_data['game_type'] = game_type
            context.user_data['is_demo'] = True
            context.user_data['bet_amount'] = 100  # Demo bet
            
            config = GAME_CONFIG[game_type]
            keyboard = [
                [InlineKeyboardButton(t("mode_normal", user_id=user_id), callback_data=f"mode_normal_{game_type}")],
                [InlineKeyboardButton(t("mode_double", user_id=user_id), callback_data=f"mode_double_{game_type}")],
                [InlineKeyboardButton(t("mode_crazy", user_id=user_id), callback_data=f"mode_crazy_{game_type}")],
                [InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="back_to_demo_menu")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"🎮 <b>DEMO: {config['name']}</b> 🔑\n\n"
                "🎲 <b>Select game mode</b>\n\n"
                "<i>• Normal mode: Highest value wins\n"
                "• Crazy mode: Lowest value wins\n"
                "• Double mode: 2 emojis are rolled in 1 round</i>\n\n"
                "(No Stars will be deducted)",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            return
        
        if data == "back_to_demo_menu":
            keyboard = [
                [
                    InlineKeyboardButton(t("demo_dice_btn", user_id=user_id), callback_data="demo_game_dice"),
                    InlineKeyboardButton(t("demo_bowl_btn", user_id=user_id), callback_data="demo_game_bowl"),
                ],
                [
                    InlineKeyboardButton(t("demo_dart_btn", user_id=user_id), callback_data="demo_game_dart"),
                    InlineKeyboardButton(t("demo_football_btn", user_id=user_id), callback_data="demo_game_football"),
                ],
                [
                    InlineKeyboardButton(t("demo_basketball_btn", user_id=user_id), callback_data="demo_game_basket"),
                ],
                [
                    InlineKeyboardButton(t("btn_cancel_demo", user_id=user_id), callback_data="cancel_demo"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"🎮 <b>DEMO MODE</b> 🔑\n\n"
                f"🎯 Choose a game to test:\n"
                f"(No Stars will be deducted)",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            return
        
        if data == "cancel_demo":
            await query.edit_message_text(
                translate_text("❌ Demo cancelled.", user_id=user_id),
                parse_mode=ParseMode.HTML
            )
            return
        
        # ===== NEW POINT-BASED GAME CALLBACKS =====
        
        # Bet selection callback
        if data.startswith("bet_"):
            parts = data.split("_")
            game_type = parts[1]
            bet_amount = int(parts[2])
            
            if game_type not in GAME_CONFIG:
                await query.answer(t("err_unknown_game", user_id=user_id), show_alert=True)
                return
            
            balance = get_user_balance(user_id)
            
            if balance < bet_amount and not is_admin(user_id):
                await query.edit_message_text(
                    "❌ Insufficient balance! Use /deposit to add Stars.",
                    parse_mode=ParseMode.HTML
                )
                return
            
            context.user_data['bet_amount'] = bet_amount
            context.user_data['game_type'] = game_type
            
            config = GAME_CONFIG[game_type]
            keyboard = [
                [InlineKeyboardButton(t("mode_normal", user_id=user_id), callback_data=f"mode_normal_{game_type}")],
                [InlineKeyboardButton(t("mode_double", user_id=user_id), callback_data=f"mode_double_{game_type}")],
                [InlineKeyboardButton(t("mode_crazy", user_id=user_id), callback_data=f"mode_crazy_{game_type}")],
                [InlineKeyboardButton(t("cancel_game", user_id=user_id), callback_data=f"cancel_{game_type}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            sent_bet = await query.edit_message_text(
                "🎲 <b>Select game mode</b>\n\n"
                "<i>• Normal mode: Highest value wins\n"
                "• Crazy mode: Lowest value wins\n"
                "• Double mode: 2 emojis are rolled in 1 round</i>",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            register_menu_owner(sent_bet, user_id)
            return
        
        # Mode selection callback
        if data.startswith("mode_"):
            parts = data.split("_")
            mode = parts[1]  # normal, double, crazy
            game_type = parts[2]
            
            if game_type not in GAME_CONFIG:
                await query.answer(t("err_unknown_game", user_id=user_id), show_alert=True)
                return
            
            context.user_data['mode'] = mode
            config = GAME_CONFIG[game_type]
            
            keyboard = [
                [InlineKeyboardButton(t("btn_up_to_1", user_id=user_id), callback_data=f"points_1_{game_type}")],
                [InlineKeyboardButton(t("btn_up_to_2", user_id=user_id), callback_data=f"points_2_{game_type}")],
                [InlineKeyboardButton(t("btn_up_to_3", user_id=user_id), callback_data=f"points_3_{game_type}")],
                [InlineKeyboardButton("↩ Back", callback_data=f"back_to_mode_{game_type}")],
                [InlineKeyboardButton("🗑 Delete", callback_data=f"cancel_{game_type}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            sent_mode = await query.edit_message_text(
                "🎲 <b>Select the number of points needed to win</b>\n\n"
                "<i>ℹ️ The first player to win the selected number of rounds wins</i>",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            register_menu_owner(sent_mode, user_id)
            return
        
        # Points selection callback
        if data.startswith("points_"):
            parts = data.split("_")
            points_target = int(parts[1])
            game_type = parts[2]
            
            if game_type not in GAME_CONFIG:
                await query.answer(t("err_unknown_game", user_id=user_id), show_alert=True)
                return
            
            bet_amount = context.user_data.get('bet_amount', 10)
            mode = context.user_data.get('mode', 'normal')
            is_demo = context.user_data.get('is_demo', False)
            config = GAME_CONFIG[game_type]
            multiplier = MULTIPLIERS[mode]
            bet_usd = bet_amount * STARS_TO_USD
            
            # Mode descriptions
            mode_display = mode.capitalize()
            if mode == "normal":
                desc = f"the one with the higher {config['action']} wins"
            elif mode == "double":
                desc = f"each player goes twice — highest total wins the round"
            elif mode == "crazy":
                desc = f"the one with the LOWER {config['action']} wins"
            else:
                desc = ""
            
            keyboard = [
                [
                    InlineKeyboardButton(f"{config['emoji']} Play now! {config['emoji']}", callback_data=f"play_{game_type}"),
                ],
                [
                    InlineKeyboardButton(t("btn_cancel_game", user_id=user_id), callback_data=f"cancel_{game_type}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            context.user_data['points_target'] = points_target
            
            demo_tag = " 🔑 DEMO" if is_demo else ""
            
            sent_pts = await query.edit_message_text(
                f"{config['emoji']} <b>{config['name']} vs 🤖 Bot</b>{demo_tag}\n\n"
                f"💰 Bet: <b>{bet_amount} ⭐</b> (${bet_usd:.2f})\n"
                f"📈 Multiplier: <b>×{multiplier}</b>\n"
                f"🎮 Mode: {mode_display} - Up to {points_target} point{'s' if points_target > 1 else ''}\n\n"
                f"Take turns {config['action']}ing {config['emoji']} — {desc}",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            register_menu_owner(sent_pts, user_id)
            return
        
        # Replay with same settings from last game
        if data.startswith("replay_"):
            game_type = data.replace("replay_", "")
            if game_type not in GAME_CONFIG:
                await query.answer(t("err_unknown_game", user_id=user_id), show_alert=True)
                return

            if user_id in game_sessions:
                await query.answer(t("err_active_game", user_id=user_id), show_alert=True)
                return

            last = user_last_game_settings.get(user_id)
            if last and last.get('game_type') == game_type:
                bet_amount = last['bet_amount']
                mode = last.get('mode', 'normal')
                points_target = last.get('points_target', 1)
            else:
                bet_amount = 10
                mode = 'normal'
                points_target = 1

            balance = get_user_balance(user_id)
            if balance < bet_amount and not is_admin(user_id):
                await query.answer(f"❌ Insufficient balance! You have {balance} ⭐", show_alert=True)
                return

            await query.answer()

            # Deduct balance
            if not is_admin(user_id):
                adjust_user_balance(user_id, -bet_amount, game=True)
                user_balances[user_id] = get_user_balance(user_id)

            multiplier = MULTIPLIERS[mode]
            config = GAME_CONFIG[game_type]

            game_sessions[user_id] = {
                "game_type": game_type,
                "mode": mode,
                "points_target": points_target,
                "player_score": 0,
                "bot_score": 0,
                "bet": bet_amount,
                "multiplier": multiplier,
                "chat_id": query.message.chat_id,
                "message_id": query.message.message_id,
                "is_demo": False,
                "player_rolls_needed": 2 if mode == "double" else 1,
                "player_rolls_done": 0,
                "player_total": 0,
                "waiting_for_player": True,
            }

            profile = get_or_create_profile(user_id)
            display_name = profile.get('display_name') or profile.get('username') or 'Player'
            user_link = get_user_link(user_id, display_name)
            bet_usd = bet_amount * STARS_TO_USD
            payout_usd = bet_usd * multiplier

            await query.edit_message_text(
                f"{config['emoji']} <b>{display_name} wants to play {config['name']}!</b>\n\n"
                f"Bet: ${bet_usd:.2f}\n"
                f"Payout: ${payout_usd:.2f} {multiplier}x\n\n"
                f"👤 {user_link}, it's your turn.",
                parse_mode=ParseMode.HTML,
                reply_markup=build_copy_turn_reply_markup(user_id, config['emoji'])
            )
            return

        # Play button callback - starts the actual game
        if data.startswith("play_") and not data.startswith("play_game_"):
            game_type = data.replace("play_", "")
            
            if game_type not in GAME_CONFIG:
                await query.answer(t("err_unknown_game", user_id=user_id), show_alert=True)
                return
            
            if user_id in game_sessions:
                await query.answer(t("err_active_game", user_id=user_id), show_alert=True)
                return
            
            bet_amount = context.user_data.get('bet_amount', 10)
            mode = context.user_data.get('mode', 'normal')
            points_target = context.user_data.get('points_target', 1)
            is_demo = context.user_data.get('is_demo', False)
            multiplier = MULTIPLIERS[mode]
            config = GAME_CONFIG[game_type]
            
            # Deduct balance
            if not is_demo and not is_admin(user_id):
                balance = get_user_balance(user_id)
                if balance < bet_amount:
                    await query.edit_message_text(
                        "❌ Insufficient balance! Use /deposit to add Stars.",
                        parse_mode=ParseMode.HTML
                    )
                    return
                adjust_user_balance(user_id, -bet_amount, game=True)
                new_balance = get_user_balance(user_id)
                expected_balance = balance - bet_amount
                if abs(new_balance - expected_balance) > 0.01:
                    set_user_balance(user_id, expected_balance)
                user_balances[user_id] = get_user_balance(user_id)
            
            # Create session
            game_sessions[user_id] = {
                "game_type": game_type,
                "mode": mode,
                "points_target": points_target,
                "player_score": 0,
                "bot_score": 0,
                "bet": bet_amount,
                "multiplier": multiplier,
                "chat_id": query.message.chat_id,
                "message_id": query.message.message_id,
                "is_demo": is_demo,
                "player_rolls_needed": 2 if mode == "double" else 1,
                "player_rolls_done": 0,
                "player_total": 0,
                "waiting_for_player": True,
            }

            # Get display name for start message
            profile = get_or_create_profile(user_id)
            display_name = profile.get('display_name') or profile.get('username') or 'Player'
            user_link = get_user_link(user_id, display_name)

            bet_usd = bet_amount * STARS_TO_USD
            payout_usd = bet_usd * multiplier

            await query.edit_message_text(
                f"{config['emoji']} <b>{display_name} wants to play {config['name']}!</b>\n\n"
                f"Bet: ${bet_usd:.2f}\n"
                f"Payout: ${payout_usd:.2f} {multiplier}x\n\n"
                f"👤 {user_link}, it's your turn.",
                parse_mode=ParseMode.HTML,
                reply_markup=build_copy_turn_reply_markup(user_id, config['emoji'])
            )
            return
        
        # ---- ââââ COINFLIP CALLBACKS ââââ ----
        if data in ("cf_heads", "cf_tails"):
            call = "heads" if data == "cf_heads" else "tails"
            bet_amount = context.user_data.get('cf_bet', 0)
            if bet_amount <= 0:
                await query.answer(t("err_invalid_bet", user_id=user_id), show_alert=True)
                return

            bet_usd = bet_amount * STARS_TO_USD
            payout_usd = bet_usd * CF_MULTIPLIER

            context.user_data['cf_call'] = call
            call_display = "Heads" if call == "heads" else "Tails"

            keyboard = [
                [
                    InlineKeyboardButton(t("btn_confirm", user_id=user_id), callback_data="cf_confirm"),
                    InlineKeyboardButton(t("btn_cancel", user_id=user_id), callback_data="cf_cancel"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"You're about to start a Coinflip Game!\n\n"
                f"<b>Call:</b> {call_display}\n"
                f"<b>Bet:</b> ${bet_usd:.2f}\n"
                f"<b>Payout:</b> ${payout_usd:.2f} {CF_MULTIPLIER}x",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            return

        if data == "cf_confirm":
            bet_amount = context.user_data.get('cf_bet', 0)
            call = context.user_data.get('cf_call', 'heads')
            if bet_amount <= 0:
                await query.answer(t("err_invalid_bet", user_id=user_id), show_alert=True)
                return

            balance = get_user_balance(user_id)
            if balance < bet_amount:
                await query.edit_message_text(t("insufficient_balance", user_id=user_id), parse_mode=ParseMode.HTML)
                return

            if user_id in coinflip_sessions:
                await query.answer(t("err_active_coinflip", user_id=user_id), show_alert=True)
                return

            # Deduct bet
            adjust_user_balance(user_id, -bet_amount, game=True)
            user_balances[user_id] = get_user_balance(user_id)

            coinflip_sessions[user_id] = {
                "call": call,
                "bet": bet_amount,
                "chat_id": query.message.chat_id,
                "message_id": query.message.message_id,
            }

            bet_usd = bet_amount * STARS_TO_USD
            payout_usd = bet_usd * CF_MULTIPLIER
            call_display = "Heads" if call == "heads" else "Tails"

            keyboard = [[InlineKeyboardButton(t("btn_flip_coin", user_id=user_id), callback_data="cf_flip")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"🤖 <b>Coinflip vs Bot</b>\n\n"
                f"<b>Your call:</b> {call_display}\n"
                f"<b>Bet:</b> ${bet_usd:.2f}\n"
                f"<b>Payout:</b> ${payout_usd:.2f} ({CF_MULTIPLIER}x)\n\n"
                f"Click to flip the coin!",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            return

        if data == "cf_flip":
            session = coinflip_sessions.get(user_id)
            if not session:
                await query.answer(t("err_no_active_coinflip", user_id=user_id), show_alert=True)
                return

            chat_id = session['chat_id']
            bet_amount = session['bet']
            call = session['call']
            bet_usd = bet_amount * STARS_TO_USD
            payout_usd = bet_usd * CF_MULTIPLIER

            # Remove flip button
            await query.edit_message_reply_markup(reply_markup=None)

            # Random outcome
            outcome = random.choice(["heads", "tails"])
            outcome_display = "Heads" if outcome == "heads" else "Tails"

            # Send sticker (fallback to text if stickers are not configured)
            sticker_id = coinflip_stickers.get(outcome)
            if sticker_id:
                await context.bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
                await asyncio.sleep(2)
            else:
                fallback_face = "🙂" if outcome == "heads" else "🪙"
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Coin result: {fallback_face} <b>{outcome_display}</b>",
                    parse_mode=ParseMode.HTML
                )
                await asyncio.sleep(1)

            # Get user display
            profile = get_or_create_profile(user_id)
            display_name = profile.get('display_name') or profile.get('username') or 'Player'

            player_won = (outcome == call)

            if player_won:
                winnings_int = int(bet_amount * CF_MULTIPLIER)
                paid = adjust_user_balance(user_id, winnings_int, game=True)
                if paid is False:
                    result_line = "🔧 <b>Casino Maintenance</b>\n\nUnable to process win right now. Please try again shortly."
                else:
                    user_balances[user_id] = get_user_balance(user_id)
                    stats_game_type = 'coinflip'
                    update_game_stats(user_id, stats_game_type, bet_amount, winnings_int, True)
                    result_line = f"🎉 <b>{display_name}</b> wins and earns <b>${payout_usd:.2f}</b> ({CF_MULTIPLIER}x)"
            else:
                update_game_stats(user_id, 'coinflip', bet_amount, 0, False)
                result_line = f"🤖 <b>Bot</b> wins and earns <b>${payout_usd:.2f}</b> ({CF_MULTIPLIER}x)"

            del coinflip_sessions[user_id]

            # Store last bet for play again
            context.user_data['cf_last_bet'] = bet_amount

            game_over_keyboard = [
                [InlineKeyboardButton(t("btn_play_again", user_id=user_id), callback_data="cf_play_again")],
                [InlineKeyboardButton(t("back_to_games", user_id=user_id), callback_data="show_games")],
            ]
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🏆 <b>Game Over!</b>\n\n"
                     f"<b>Outcome:</b> {outcome_display}\n\n"
                     f"{result_line}",
                reply_markup=InlineKeyboardMarkup(game_over_keyboard),
                parse_mode=ParseMode.HTML
            )
            return

        if data == "cf_cancel":
            # Refund if bet was already deducted
            if user_id in coinflip_sessions:
                session = coinflip_sessions[user_id]
                adjust_user_balance(user_id, session['bet'])
                user_balances[user_id] = get_user_balance(user_id)
                del coinflip_sessions[user_id]

            context.user_data.pop('cf_bet', None)
            context.user_data.pop('cf_call', None)

            await query.edit_message_text(t("cf_cancelled", user_id=user_id), parse_mode=ParseMode.HTML)
            return

        if data == "cf_play_again":
            last_bet = context.user_data.get('cf_last_bet', 0)
            if last_bet <= 0:
                await query.answer()
                await query.edit_message_text(
                    "🎲 <b>Coinflip</b>\n\nUse /cf <amount> to play!",
                    parse_mode=ParseMode.HTML
                )
                return

            if user_id in coinflip_sessions:
                await query.answer(t("err_active_coinflip", user_id=user_id), show_alert=True)
                return

            balance = get_user_balance(user_id)
            if balance < last_bet:
                await query.answer(f"❌ Insufficient balance! You have {balance} ⭐", show_alert=True)
                return

            await query.answer()
            context.user_data['cf_bet'] = last_bet

            keyboard = [
                [
                    InlineKeyboardButton(t("cf_heads", user_id=user_id), callback_data="cf_heads"),
                    InlineKeyboardButton(t("cf_tails", user_id=user_id), callback_data="cf_tails"),
                ],
                [InlineKeyboardButton(t("btn_cancel", user_id=user_id), callback_data="cf_cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"🎲 <b>Coinflip — {last_bet} ⭐</b>\n\n"
                f"Call the side you believe the coin will land on:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            return

        # Copy emoji button fallback (for older telegram lib without CopyTextButton)
        if data.startswith("copy_emoji_"):
            game_type = data.replace("copy_emoji_", "")
            config = GAME_CONFIG.get(game_type)
            if config:
                await query.answer(f"Send this emoji: {config['tg_emoji']}", show_alert=True)
            return
        
        # Cashout button callback — end game early, return partial bet
        if data.startswith("cashout_"):
            game_type = data.replace("cashout_", "")
            
            if user_id not in game_sessions:
                await query.answer(t("err_no_active_game", user_id=user_id), show_alert=True)
                return
            
            session = game_sessions[user_id]
            if session['game_type'] != game_type:
                await query.answer(t("err_game_mismatch", user_id=user_id), show_alert=True)
                return
            
            config = GAME_CONFIG[game_type]
            bet = session['bet']
            target = session['points_target']
            b_score = session['bot_score']
            p_score = session['player_score']
            is_demo = session.get('is_demo', False)
            
            # Calculate cashout amount
            cashout_stars = int(bet * (target - b_score) / target)
            if cashout_stars < 1:
                cashout_stars = 1
            cashout_usd = cashout_stars * STARS_TO_USD
            
            # Credit cashout to user
            if not is_demo and not is_admin(user_id):
                adjust_user_balance(user_id, cashout_stars, game=True)
                user_balances[user_id] = get_user_balance(user_id)
            
            # Record stats
            if not is_demo:
                stats_game_type = 'arrow' if game_type == 'dart' else game_type
                update_game_stats(user_id, stats_game_type, bet, cashout_stars, cashout_stars > bet)
            
            # Get user display
            profile = get_or_create_profile(user_id)
            display_name = profile.get('display_name') or profile.get('username') or 'Player'
            user_link = get_user_link(user_id, display_name)
            
            # Clean up session
            del game_sessions[user_id]
            
            balance = get_user_balance(user_id)
            
            await query.edit_message_text(
                f"💸 <b>{display_name} cashed out!</b>\n\n"
                f"<b>Scores:</b>\n"
                f"👤 Bot • <b>{b_score}</b>\n"
                f"👤 {user_link} • <b>{p_score}</b>\n\n"
                f"💸 <b>{display_name}</b> cashes out and receives <b>${cashout_usd:.2f}</b>\n\n"
                f"💰 Balance: <b>{balance:,} ⭐</b>",
                parse_mode=ParseMode.HTML
            )
            return
        
        # Cancel game callback
        if data.startswith("cancel_"):
            cancel_game_type = data.replace("cancel_", "")
            
            if user_id in game_sessions:
                session = game_sessions[user_id]
                # Refund bet
                if not session.get('is_demo', False) and not is_admin(user_id):
                    adjust_user_balance(user_id, session['bet'])
                    user_balances[user_id] = get_user_balance(user_id)
                del game_sessions[user_id]
            
            await query.edit_message_text(
                translate_text("❌ Game cancelled.", user_id=user_id),
                parse_mode=ParseMode.HTML
            )
            return
            
    except Exception as e:
        logger.error(f"Button callback error: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                translate_text("❌ An error occurred. Please try again.", user_id=user_id),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass


# ============================================================
# PREDICT GAME (Dice Number Prediction)
# ============================================================

def predict_get_multiplier(selected, selection_type):
    """Calculate multiplier based on selection count with house edge"""
    if selection_type in ("even", "odd", "low", "high"):
        count = 3
    else:
        count = len(selected)
    if count == 0 or count >= 6:
        return 0.0
    raw = 6.0 / count
    mult = round(raw * (1 - PREDICT_HOUSE_EDGE), 2)
    return mult


def predict_build_message(user_id, session):
    """Build the predict game message text"""
    selected = session.get('selected', set())
    selection_type = session.get('selection_type')
    bet = session.get('bet', PREDICT_DEFAULT_BET)
    balance = get_user_balance(user_id)
    balance_usd = balance * STARS_TO_USD

    mult = predict_get_multiplier(selected, selection_type)

    # Format selected display
    if selection_type == "even":
        sel_display = "Even (2, 4, 6)"
    elif selection_type == "odd":
        sel_display = "Odd (1, 3, 5)"
    elif selection_type == "low":
        sel_display = "1-3"
    elif selection_type == "high":
        sel_display = "4-6"
    elif selected:
        sel_display = " ".join(str(n) for n in sorted(selected))
    else:
        sel_display = "None"

    bet_usd = bet * STARS_TO_USD

    text = (
        f"🎲 <b>Make a prediction for number outcomes</b>\n\n"
        f"🔵 Multiplier: <b>x{mult:.2f}</b>\n"
        f"🔥 Selected numbers: <b>{sel_display}</b>\n"
        f"💰 Bet: <b>${bet_usd:.2f}</b> ({bet} ⭐)\n"
        f"🧿 Current balance: <b>${balance_usd:.2f}</b> ({balance:,} ⭐)"
    )
    return text


def predict_build_keyboard(session, user_id=None):
    """Build the predict game inline keyboard"""
    selected = session.get('selected', set())
    selection_type = session.get('selection_type')

    def num_label(n):
        if selection_type == "even" and n % 2 == 0:
            return f"✅ {n}"
        elif selection_type == "odd" and n % 2 == 1:
            return f"✅ {n}"
        elif selection_type == "low" and n <= 3:
            return f"✅ {n}"
        elif selection_type == "high" and n >= 4:
            return f"✅ {n}"
        elif n in selected and selection_type is None:
            return f"✅ {n}"
        return str(n)

    keyboard = [
        [
            InlineKeyboardButton(num_label(1), callback_data="pred_num_1"),
            InlineKeyboardButton(num_label(2), callback_data="pred_num_2"),
            InlineKeyboardButton(num_label(3), callback_data="pred_num_3"),
        ],
        [
            InlineKeyboardButton(num_label(4), callback_data="pred_num_4"),
            InlineKeyboardButton(num_label(5), callback_data="pred_num_5"),
            InlineKeyboardButton(num_label(6), callback_data="pred_num_6"),
        ],
        [
            InlineKeyboardButton(("✅ " if selection_type == "even" else "") + t("btn_even", user_id=user_id), callback_data="pred_even"),
            InlineKeyboardButton(("✅ " if selection_type == "odd" else "") + t("btn_odd", user_id=user_id), callback_data="pred_odd"),
        ],
        [
            InlineKeyboardButton("✅ 1-3" if selection_type == "low" else "1-3", callback_data="pred_low"),
            InlineKeyboardButton("✅ 4-6" if selection_type == "high" else "4-6", callback_data="pred_high"),
        ],
        [
            InlineKeyboardButton(t("btn_play", user_id=user_id), callback_data="pred_play"),
        ],
        [
            InlineKeyboardButton(t("btn_change_bet", user_id=user_id), callback_data="pred_change_bet"),
        ],
        [
            InlineKeyboardButton(t("btn_cancel_game2", user_id=user_id), callback_data="pred_cancel"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


@handle_errors
async def predict_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /predict command"""
    user_id = update.effective_user.id

    if is_banned(user_id) and not is_admin(user_id):
        return

    if user_id in predict_sessions:
        await update.message.reply_html(t("pred_active", user_id=user_id))
        return

    if user_id in game_sessions:
        await update.message.reply_html(t("active_game", user_id=user_id))
        return

    balance = get_user_balance(user_id)

    # Parse bet from args
    bet = PREDICT_DEFAULT_BET
    if context.args and len(context.args) > 0:
        arg = context.args[0].lower()
        if arg == 'all':
            bet = int(balance)
        elif arg == 'half':
            bet = int(balance / 2)
        else:
            try:
                bet = int(arg)
            except ValueError:
                await update.message.reply_html(t("invalid_bet_amount", user_id=user_id))
                return

    if bet < PREDICT_MIN_BET:
        bet = PREDICT_MIN_BET

    if balance < bet and not is_admin(user_id):
        await update.message.reply_html(
            f"❌ Insufficient balance!\n"
            f"Your balance: <b>{balance} ⭐</b>\n"
            f"Minimum bet: <b>{PREDICT_MIN_BET} ⭐</b>"
        )
        return

    session = {
        'chat_id': update.effective_chat.id,
        'message_id': None,
        'selected': set(),
        'selection_type': None,
        'bet': bet,
    }
    predict_sessions[user_id] = session

    text = predict_build_message(user_id, session)
    keyboard = predict_build_keyboard(session, user_id=user_id)

    sent = await update.message.reply_html(text, reply_markup=keyboard)
    session['message_id'] = sent.message_id
    register_menu_owner(sent, user_id)


async def handle_predict_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all predict game callbacks"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    session = predict_sessions.get(user_id)
    if not session:
        await query.answer(t("err_no_predict", user_id=user_id), show_alert=True)
        return

    # --- Number toggle ---
    if data.startswith("pred_num_"):
        num = int(data.split("_")[-1])
        # If a special selection type was active, switch to manual
        if session['selection_type'] is not None:
            session['selection_type'] = None
            session['selected'] = set()

        if num in session['selected']:
            session['selected'].discard(num)
        else:
            if len(session['selected']) >= 5:
                await query.answer(t("err_max_5_nums", user_id=user_id), show_alert=True)
                return
            session['selected'].add(num)

        # Block selecting all 6
        if session['selected'] == {1, 2, 3, 4, 5, 6}:
            session['selected'].discard(num)
            await query.answer(t("err_cant_all_6", user_id=user_id), show_alert=True)
            return

        text = predict_build_message(user_id, session)
        keyboard = predict_build_keyboard(session, user_id=user_id)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await query.answer()
        return

    # --- Even / Odd / Low / High ---
    if data in ("pred_even", "pred_odd", "pred_low", "pred_high"):
        type_map = {"pred_even": "even", "pred_odd": "odd", "pred_low": "low", "pred_high": "high"}
        new_type = type_map[data]
        if session['selection_type'] == new_type:
            session['selection_type'] = None
            session['selected'] = set()
        else:
            session['selection_type'] = new_type
            session['selected'] = set()

        text = predict_build_message(user_id, session)
        keyboard = predict_build_keyboard(session, user_id=user_id)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await query.answer()
        return

    # --- Change Bet ---
    if data == "pred_change_bet":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("5 ⭐", callback_data="pred_bet_5"),
                InlineKeyboardButton("10 ⭐", callback_data="pred_bet_10"),
                InlineKeyboardButton("25 ⭐", callback_data="pred_bet_25"),
            ],
            [
                InlineKeyboardButton("50 ⭐", callback_data="pred_bet_50"),
                InlineKeyboardButton("100 ⭐", callback_data="pred_bet_100"),
                InlineKeyboardButton(t("btn_all_in", user_id=user_id), callback_data="pred_bet_all"),
            ],
            [
                InlineKeyboardButton(t("back_button", user_id=user_id), callback_data="pred_bet_back"),
            ],
        ])
        await query.edit_message_text(
            "📝 <b>Change your bet amount:</b>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        await query.answer()
        return

    if data.startswith("pred_bet_"):
        bet_val = data.replace("pred_bet_", "")
        if bet_val == "back":
            text = predict_build_message(user_id, session)
            keyboard = predict_build_keyboard(session, user_id=user_id)
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            await query.answer()
            return

        balance = get_user_balance(user_id)
        if bet_val == "all":
            new_bet = int(balance)
        else:
            new_bet = int(bet_val)

        if new_bet < PREDICT_MIN_BET:
            new_bet = PREDICT_MIN_BET

        if new_bet > balance and not is_admin(user_id):
            await query.answer(f"❌ Insufficient balance! You have {balance} ⭐", show_alert=True)
            return

        session['bet'] = new_bet
        text = predict_build_message(user_id, session)
        keyboard = predict_build_keyboard(session, user_id=user_id)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await query.answer()
        return

    # --- Cancel ---
    if data == "pred_cancel":
        del predict_sessions[user_id]
        await query.edit_message_text("🔴 Predict game cancelled.", parse_mode=ParseMode.HTML)
        await query.answer()
        return

    # --- Play ---
    if data == "pred_play":
        selected = session['selected']
        selection_type = session['selection_type']

        # Validate selection
        has_selection = bool(selected) or selection_type is not None
        if not has_selection:
            await query.answer(t("err_select_at_least_one", user_id=user_id), show_alert=True)
            return

        bet = session['bet']
        balance = get_user_balance(user_id)

        if bet > balance and not is_admin(user_id):
            await query.answer(f"❌ Insufficient balance! You have {balance} ⭐", show_alert=True)
            return

        # Deduct bet
        adjust_user_balance(user_id, -bet, game=True)

        profile = get_or_create_profile(user_id)
        display_name = profile.get('display_name') or profile.get('username') or 'Player'
        user_link = get_user_link(user_id, display_name)

        # Show rolling message
        await query.edit_message_text(
            f"🎲 Rolling dice for {user_link}...",
            parse_mode=ParseMode.HTML
        )

        # Send dice animation
        chat_id = session['chat_id']
        dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji="🎲")
        dice_value = dice_msg.dice.value

        # Wait for animation
        await asyncio.sleep(2.5)

        # Determine win/loss
        winning_numbers = set()
        if selection_type == "even":
            winning_numbers = {2, 4, 6}
        elif selection_type == "odd":
            winning_numbers = {1, 3, 5}
        elif selection_type == "low":
            winning_numbers = {1, 2, 3}
        elif selection_type == "high":
            winning_numbers = {4, 5, 6}
        else:
            winning_numbers = set(selected)

        mult = predict_get_multiplier(selected, selection_type)
        won = dice_value in winning_numbers
        win_amount = 0

        if won:
            win_amount = int(round(bet * mult))
            adjust_user_balance(user_id, win_amount, game=True)

        new_balance = get_user_balance(user_id)
        new_balance_usd = new_balance * STARS_TO_USD
        win_usd = win_amount * STARS_TO_USD

        # Update stats
        update_game_stats(user_id, 'predict', bet, win_amount if won else 0, won)

        # Build result message
        if won:
            result_text = (
                f"🏆 {user_link} wins!\n\n"
                f"🎲 Result: <b>{dice_value}</b>\n"
                f"💸 Win: <b>${win_usd:.2f}</b> ({win_amount} ⭐)\n"
                f"🧿 Current balance: <b>${new_balance_usd:.2f}</b> ({new_balance:,} ⭐)"
            )
        else:
            result_text = (
                f"❌ {user_link} lost!\n\n"
                f"🎲 Result: <b>{dice_value}</b>\n"
                f"💸 Win: <b>$0.00</b>\n"
                f"🧿 Current balance: <b>${new_balance_usd:.2f}</b> ({new_balance:,} ⭐)"
            )

        # Send result as a separate message (not edit) so it appears after the dice
        await context.bot.send_message(
            chat_id=chat_id,
            text=result_text,
            parse_mode=ParseMode.HTML
        )

        # Clean up session
        del predict_sessions[user_id]
        return


# ============================================================
# COINFLIP GAME
# ============================================================

@handle_errors
async def cflip_setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    cflip_setup[user_id] = {"step": "heads"}
    await update.message.reply_html(t("cf_setup_send_heads", user_id=user_id))


@handle_errors
async def handle_cflip_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in cflip_setup:
        return

    sticker = update.message.sticker
    if not sticker:
        return

    step = cflip_setup[user_id]["step"]

    if step == "heads":
        coinflip_stickers["heads"] = sticker.file_id
        cflip_setup[user_id]["step"] = "tails"
        await update.message.reply_html(t("cf_setup_heads_saved", user_id=user_id))
    elif step == "tails":
        coinflip_stickers["tails"] = sticker.file_id
        save_coinflip_stickers()
        del cflip_setup[user_id]
        await update.message.reply_html(t("cf_setup_complete", user_id=user_id))


@handle_errors
async def cf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ''

    if is_banned(user_id):
        return

    get_or_create_profile(user_id, username)

    # Keep /cf playable even when custom stickers are not configured.

    if user_id in coinflip_sessions:
        await update.message.reply_html(t("cf_active", user_id=user_id))
        return

    if user_id in game_sessions:
        await update.message.reply_html(t("finish_current_game", user_id=user_id))
        return

    args = context.args
    if not args:
        await update.message.reply_html(t("cf_usage", user_id=user_id))
        return

    balance = get_user_balance(user_id)
    arg = args[0].lower()

    if arg == "all":
        bet_amount = balance
    elif arg == "half":
        bet_amount = balance // 2
    else:
        try:
            bet_amount = int(arg)
        except ValueError:
            await update.message.reply_html(t("invalid_amount", user_id=user_id))
            return

    if bet_amount <= 0:
        await update.message.reply_html(t("bet_greater_than_zero", user_id=user_id))
        return

    if balance < bet_amount:
        await update.message.reply_html(
            f"❌ Insufficient balance!\n💰 Your balance: <b>{balance:,} ⭐</b>",
        )
        return

    context.user_data['cf_bet'] = bet_amount

    keyboard = [
        [
            InlineKeyboardButton(t("cf_heads", user_id=user_id), callback_data="cf_heads"),
            InlineKeyboardButton(t("cf_tails", user_id=user_id), callback_data="cf_tails"),
        ],
        [InlineKeyboardButton(t("cancel_button", user_id=user_id), callback_data="cf_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    sent = await update.message.reply_html(
        t("cf_call_side", user_id=user_id),
        reply_markup=reply_markup
    )
    register_menu_owner(sent, user_id)


# ==================== BLACKJACK GAME LOGIC ====================

def bj_create_deck():
    """Create and shuffle a full 52-card deck."""
    deck = []
    for suit in BJ_SUITS:
        for value in BJ_VALUES:
            deck.append({"suit": suit, "value": value})
    random.shuffle(deck)
    return deck


def bj_card_points(value):
    """Get point value of a card."""
    if value in ["J", "Q", "K"]:
        return 10
    elif value == "A":
        return 11
    else:
        return int(value)


def bj_calculate_score(cards):
    """Calculate hand score with ace logic."""
    score = 0
    aces = 0
    for card in cards:
        pts = bj_card_points(card["value"])
        score += pts
        if card["value"] == "A":
            aces += 1
    while score > 21 and aces:
        score -= 10
        aces -= 1
    return score


def bj_calculate_visible_score(bot_cards, hide_second=True):
    """Calculate visible score (hide second card if needed)."""
    if hide_second and len(bot_cards) > 1:
        visible = [bot_cards[0]]
    else:
        visible = bot_cards
    return bj_calculate_score(visible)


def bj_hand_str(cards):
    """Return human-readable hand string like 'K ♠ Q ♥'."""
    return " ".join(f"{c['value']}{c['suit']}" for c in cards)


def bj_generate_table_image(player_cards, bot_cards, hide_bot_second=True, result_text=None):
    """Generate a casino felt table image with cards."""
    width, height = 800, 500
    img = Image.new("RGB", (width, height), (25, 100, 50))
    draw = ImageDraw.Draw(img)

    # Felt texture - subtle gradient
    for y_pos in range(height):
        shade = int(25 + 8 * (y_pos / height))
        g = int(100 + 20 * (y_pos / height))
        for x_pos in range(0, width, 4):
            draw.point((x_pos, y_pos), fill=(shade, g, shade))

    # Gold wooden frame border
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=(139, 101, 42), width=8)
    draw.rectangle([(8, 8), (width - 9, height - 9)], outline=(184, 134, 11), width=3)
    draw.rectangle([(12, 12), (width - 13, height - 13)], outline=(218, 165, 32), width=1)

    # Try to load a nice font, fall back to default
    try:
        font_large = ImageFont.truetype("arial.ttf", 22)
        font_medium = ImageFont.truetype("arial.ttf", 18)
        font_small = ImageFont.truetype("arial.ttf", 14)
        font_suit_big = ImageFont.truetype("arial.ttf", 30)
        font_score = ImageFont.truetype("arialbd.ttf", 20)
    except (OSError, IOError):
        try:
            font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
            font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
            font_suit_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
            font_score = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        except (OSError, IOError):
            font_large = ImageFont.load_default()
            font_medium = font_large
            font_small = font_large
            font_suit_big = font_large
            font_score = font_large

    def draw_card(x, y, value, suit, hidden=False, glow=False):
        card_w, card_h = 90, 120

        if glow:
            draw.rectangle(
                [(x - 4, y - 4), (x + card_w + 4, y + card_h + 4)],
                outline=(0, 255, 100), width=3
            )

        if hidden:
            # Face down card
            draw.rectangle([(x, y), (x + card_w, y + card_h)],
                           fill=(20, 40, 120), outline=(200, 200, 200), width=2)
            draw.rectangle([(x + 8, y + 8), (x + card_w - 8, y + card_h - 8)],
                           outline=(100, 100, 200), width=1)
            # Diamond pattern on back
            cx, cy_c = x + card_w // 2, y + card_h // 2
            draw.polygon([(cx, cy_c - 25), (cx + 18, cy_c), (cx, cy_c + 25), (cx - 18, cy_c)],
                         outline=(80, 80, 180), fill=(30, 50, 140))
        else:
            # Face up card - white with rounded feel
            draw.rectangle([(x, y), (x + card_w, y + card_h)],
                           fill=(255, 255, 255), outline=(180, 180, 180), width=2)

            suit_colors = {"♠": (0, 0, 0), "♣": (0, 0, 0), "♥": (200, 0, 0), "♦": (200, 0, 0)}
            rgb = suit_colors.get(suit, (0, 0, 0))

            # Value top left
            draw.text((x + 8, y + 6), value, fill=rgb, font=font_medium)
            draw.text((x + 8, y + 26), suit, fill=rgb, font=font_small)

            # Value bottom right
            draw.text((x + card_w - 24, y + card_h - 40), value, fill=rgb, font=font_medium)
            draw.text((x + card_w - 22, y + card_h - 22), suit, fill=rgb, font=font_small)

            # Large suit center
            draw.text((x + card_w // 2, y + card_h // 2), suit, fill=rgb, font=font_suit_big, anchor="mm")

    def draw_score_bubble(cx, cy, score, color=(50, 50, 50)):
        bubble_w = 70
        bubble_h = 30
        x1 = cx - bubble_w // 2
        y1 = cy - bubble_h // 2
        x2 = cx + bubble_w // 2
        y2 = cy + bubble_h // 2
        draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=15, fill=color)
        draw.text((cx, cy), str(score), fill="white", font=font_score, anchor="mm")

    # "DEALER" label
    draw.text((width // 2, 30), "DEALER", fill=(200, 200, 200), font=font_medium, anchor="mm")

    # BOT CARDS - top area
    bot_start_x = width // 2 - (len(bot_cards) * 100) // 2
    for i, card in enumerate(bot_cards):
        cx = bot_start_x + i * 100
        cy = 55
        hidden = (i == 1 and hide_bot_second)
        draw_card(cx, cy, card["value"], card["suit"], hidden=hidden)

    # Bot score bubble
    visible_score = bj_calculate_visible_score(bot_cards, hide_bot_second)
    score_label = str(visible_score) if not (hide_bot_second and len(bot_cards) > 1) else f"{visible_score}+?"
    draw_score_bubble(width // 2, 195, score_label, color=(50, 50, 50))

    # "PLAYER" label
    draw.text((width // 2, 240), "PLAYER", fill=(200, 200, 200), font=font_medium, anchor="mm")

    # PLAYER CARDS - bottom area with green glow
    player_start_x = width // 2 - (len(player_cards) * 100) // 2
    for i, card in enumerate(player_cards):
        cx = player_start_x + i * 100
        cy = 260
        draw_card(cx, cy, card["value"], card["suit"], glow=True)

    # Player score bubble
    player_score = bj_calculate_score(player_cards)
    draw_score_bubble(width // 2, 400, player_score, color=(0, 150, 80))

    # Result banner if game is over
    if result_text:
        # Semi-transparent overlay band
        overlay = Image.new("RGBA", (width, 50), (0, 0, 0, 160))
        img_rgba = img.convert("RGBA")
        img_rgba.paste(overlay, (0, height // 2 - 25), overlay)
        img = img_rgba.convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.text((width // 2, height // 2), result_text, fill=(255, 255, 100), font=font_large, anchor="mm")

    output = BytesIO()
    img.save(output, format="PNG")
    output.seek(0)
    return output


def bj_resolve(player_score, bot_score, bet, is_natural_bj=False):
    """Resolve blackjack game. Returns (result_type, payout).
    payout is total amount returned to player (0 = lost everything)."""
    if player_score > 21:
        return "bust", 0
    if is_natural_bj:
        if bot_score == 21:
            return "push", bet
        return "blackjack", int(bet * 2.5)
    if bot_score > 21:
        return "win", bet * 2
    if player_score > bot_score:
        return "win", bet * 2
    if player_score == bot_score:
        return "push", bet
    return "loss", 0


def bj_action_buttons(session, user_id=None):
    """Build inline keyboard for current game state."""
    player_cards = session["player_cards"]
    has_two = len(player_cards) == 2
    can_split = has_two and player_cards[0]["value"] == player_cards[1]["value"]
    # For split hands, no further split/double
    is_split_hand = session.get("split_hand_index") is not None

    row1 = [
        InlineKeyboardButton(t("bj_hit", user_id=user_id), callback_data="bj_hit"),
        InlineKeyboardButton(t("bj_stand", user_id=user_id), callback_data="bj_stand"),
    ]
    row2 = []
    if has_two and not is_split_hand:
        row2.append(InlineKeyboardButton(t("bj_double", user_id=user_id), callback_data="bj_double"))
    if can_split and not is_split_hand:
        row2.append(InlineKeyboardButton(t("bj_split", user_id=user_id), callback_data="bj_split"))
    row3 = [InlineKeyboardButton(t("bj_forfeit_btn", user_id=user_id), callback_data="bj_forfeit")]

    keyboard = [row1]
    if row2:
        keyboard.append(row2)
    keyboard.append(row3)
    return InlineKeyboardMarkup(keyboard)


async def bj_send_table(context, session, hide_bot_second=True, result_text=None, reply_markup=None, caption=None):
    """Generate table image and send/edit the game message."""
    img = bj_generate_table_image(
        session["player_cards"], session["bot_cards"],
        hide_bot_second=hide_bot_second, result_text=result_text
    )
    chat_id = session["chat_id"]
    msg_id = session.get("message_id")

    if msg_id:
        # Try to edit existing photo message
        try:
            from telegram import InputMediaPhoto
            media = InputMediaPhoto(media=img, caption=caption, parse_mode=ParseMode.HTML if caption else None)
            msg = await context.bot.edit_message_media(
                chat_id=chat_id, message_id=msg_id,
                media=media, reply_markup=reply_markup
            )
            session["message_id"] = msg.message_id
            return msg
        except Exception as e:
            logger.debug(f"BJ edit_message_media failed, sending new: {e}")

    # Send new photo
    msg = await context.bot.send_photo(
        chat_id=chat_id, photo=img, caption=caption,
        parse_mode=ParseMode.HTML if caption else None,
        reply_markup=reply_markup
    )
    session["message_id"] = msg.message_id
    return msg


async def bj_finish_game(context, session, user_id, result_type, payout):
    """Handle end-of-game: credit winnings, send final image, show result."""
    bet = session["bet"]
    player_score = bj_calculate_score(session["player_cards"])
    bot_score = bj_calculate_score(session["bot_cards"])
    player_hand = bj_hand_str(session["player_cards"])
    bot_hand = bj_hand_str(session["bot_cards"])

    if payout > 0:
        adjust_user_balance(user_id, payout, game=True)
        user_balances[user_id] = get_user_balance(user_id)

    profit = payout - bet
    is_win = result_type in ("win", "blackjack")
    update_game_stats(user_id, 'blackjack', bet, payout if is_win else 0, is_win)

    # Result banner text for image
    banner_map = {
        "blackjack": "BLACKJACK!",
        "win": "YOU WIN!",
        "loss": "YOU LOSE",
        "bust": "BUSTED!",
        "push": "PUSH - TIE",
        "forfeit": "FORFEITED",
    }
    banner = banner_map.get(result_type, "GAME OVER")

    # Send final table image
    await bj_send_table(context, session, hide_bot_second=False, result_text=banner)

    # Build result caption message
    if result_type == "blackjack":
        text = (
            f"ð <b>BLACKJACK!</b> 🎉\n\n"
            f"Your hand: {player_hand} = {player_score}\n"
            f"Bot hand: {bot_hand} = {bot_score}\n\n"
            f"💰 Blackjack pays 1.5x! You earned: <b>{payout} ⭐</b>"
        )
    elif result_type == "win":
        text = (
            f"ð <b>Blackjack Result</b>\n"
            f"✅ <b>You Win!</b>\n\n"
            f"Your hand: {player_hand} = {player_score}\n"
            f"Bot hand: {bot_hand} = {bot_score}"
            + (f" (Bust!)" if bot_score > 21 else "") +
            f"\n\n💰 You earned: <b>{payout} ⭐</b>"
        )
    elif result_type == "bust":
        text = (
            f"ð <b>Blackjack Result</b>\n"
            f"❌ <b>You Busted!</b>\n\n"
            f"Your hand: {player_hand} = {player_score}\n"
            f"Bot hand: {bot_hand} = {bot_score}\n\n"
            f"💸 You lost: <b>{bet} ⭐</b>"
        )
    elif result_type == "loss":
        text = (
            f"ð <b>Blackjack Result</b>\n"
            f"❌ <b>You Lose!</b>\n\n"
            f"Your hand: {player_hand} = {player_score}\n"
            f"Bot hand: {bot_hand} = {bot_score}\n\n"
            f"💸 You lost: <b>{bet} ⭐</b>"
        )
    elif result_type == "push":
        text = (
            f"ð <b>Push! It's a tie.</b>\n\n"
            f"Your hand: {player_hand} = {player_score}\n"
            f"Bot hand: {bot_hand} = {bot_score}\n\n"
            f"↩️ Bet returned: <b>{bet} ⭐</b>"
        )
    elif result_type == "forfeit":
        returned = payout
        text = (
            f"ð <b>Blackjack Result</b>\n"
            f"🔴 <b>Forfeited</b>\n\n"
            f"Your hand: {player_hand} = {player_score}\n\n"
            f"↩️ Half bet returned: <b>{returned} ⭐</b>"
        )
    else:
        text = f"ð Game Over\n💰 Payout: {payout} â­"

    # Play again button
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(t("btn_play_again", user_id=user_id), callback_data="bj_play_again")]
    ])

    await context.bot.send_message(
        chat_id=session["chat_id"],
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )

    # Clean up session
    if user_id in blackjack_sessions:
        del blackjack_sessions[user_id]


@handle_errors
async def blackjack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /bj command - show bet selection."""
    if not update.message:
        return
    user_id = update.effective_user.id
    username = update.effective_user.username or ''

    if is_banned(user_id):
        return

    get_or_create_profile(user_id, username)

    if user_id in blackjack_sessions:
        await update.message.reply_html(t("bj_active_game2", user_id=user_id))
        return

    balance = get_user_balance(user_id)
    if balance <= 0:
        await update.message.reply_html(t("bj_insufficient", user_id=user_id))
        return

    # Check for inline amount: /bj 100
    if context.args:
        try:
            bet = int(context.args[0])
            if bet < 10:
                await update.message.reply_html(t("bj_min_bet", user_id=user_id))
                return
            if balance < bet:
                await update.message.reply_html(t("bj_insufficient2", user_id=user_id, balance=balance))
                return
            await bj_start_game(context, update, user_id, bet)
            return
        except ValueError:
            pass

    # Build bet menu
    keyboard = [
        [
            InlineKeyboardButton("50 ⭐", callback_data="bj_bet_50"),
            InlineKeyboardButton("100 ⭐", callback_data="bj_bet_100"),
            InlineKeyboardButton("250 ⭐", callback_data="bj_bet_250"),
        ],
        [
            InlineKeyboardButton("500 ⭐", callback_data="bj_bet_500"),
            InlineKeyboardButton("1000 ⭐", callback_data="bj_bet_1000"),
            InlineKeyboardButton(t("btn_custom_bet", user_id=user_id), callback_data="bj_bet_custom"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        f"ð <b>Blackjack</b>\n\n"
        f"Select your bet amount:\n\n"
        f"💳 Balance: <b>{balance} ⭐</b>"
    )

    sent = await update.message.reply_html(text, reply_markup=reply_markup)
    register_menu_owner(sent, user_id)


async def bj_start_game(context, update_or_none, user_id, bet):
    """Start a new blackjack game after bet is confirmed."""
    balance = get_user_balance(user_id)
    if balance < bet:
        return

    if user_id in blackjack_sessions:
        return

    # Deduct bet
    adjust_user_balance(user_id, -bet, game=True)
    user_balances[user_id] = get_user_balance(user_id)

    # Create deck and deal
    deck = bj_create_deck()
    player_cards = [deck.pop(), deck.pop()]
    bot_cards = [deck.pop(), deck.pop()]

    session = {
        "deck": deck,
        "player_cards": player_cards,
        "bot_cards": bot_cards,
        "bet": bet,
        "state": "playing",
        "message_id": None,
        "chat_id": None,
        "user_id": user_id,
    }

    # Determine chat_id from update context
    if update_or_none and hasattr(update_or_none, 'effective_chat'):
        session["chat_id"] = update_or_none.effective_chat.id
    elif update_or_none and hasattr(update_or_none, 'message') and update_or_none.message:
        session["chat_id"] = update_or_none.message.chat_id
    elif update_or_none and hasattr(update_or_none, 'callback_query') and update_or_none.callback_query:
        session["chat_id"] = update_or_none.callback_query.message.chat_id

    blackjack_sessions[user_id] = session

    # Check for natural blackjack
    player_score = bj_calculate_score(player_cards)
    is_natural = player_score == 21 and len(player_cards) == 2

    if is_natural:
        bot_score = bj_calculate_score(bot_cards)
        result_type, payout = bj_resolve(player_score, bot_score, bet, is_natural_bj=True)

        # Send final image immediately
        msg = await bj_send_table(context, session, hide_bot_second=False)
        session["message_id"] = msg.message_id
        await bj_finish_game(context, session, user_id, result_type, payout)
        return

    # Normal game: send table image with action buttons
    reply_markup = bj_action_buttons(session)
    caption = f"ð Blackjack | Bet: {bet} â­ | Your turn!"
    msg = await bj_send_table(context, session, hide_bot_second=True, reply_markup=reply_markup, caption=caption)
    session["message_id"] = msg.message_id
    register_menu_owner(msg, user_id)


async def bj_bot_turn(context, session, user_id):
    """Execute bot's turn: reveal card, draw until 17+, resolve."""
    session["state"] = "bot_turn"

    # Reveal hidden card
    await bj_send_table(context, session, hide_bot_second=False,
                        caption="🤖 Bot's turn...")

    await asyncio.sleep(1)

    # Bot draws until 17+
    while bj_calculate_score(session["bot_cards"]) < 17:
        session["bot_cards"].append(session["deck"].pop())
        bot_score = bj_calculate_score(session["bot_cards"])
        await bj_send_table(context, session, hide_bot_second=False,
                            caption=f"🤖 Bot draws... ({bot_score})")
        await asyncio.sleep(1)

    # Resolve
    player_score = bj_calculate_score(session["player_cards"])
    bot_score = bj_calculate_score(session["bot_cards"])
    result_type, payout = bj_resolve(player_score, bot_score, session["bet"])
    await bj_finish_game(context, session, user_id, result_type, payout)


async def bj_advance_split(context, session, user_id):
    """After finishing a split hand, advance to next hand or resolve both."""
    idx = session["split_hand_index"]
    # Save current hand score for result
    score = bj_calculate_score(session["player_cards"])
    session["split_results"][idx] = score

    if idx == 0:
        # Switch to hand 2
        session["split_hand_index"] = 1
        session["player_cards"] = session["split_hands"][1]
        session["bet"] = session["split_bets"][1]
        session["state"] = "playing"
        session["message_id"] = None  # Force new message for hand 2

        reply_markup = bj_action_buttons(session)
        hand2_score = bj_calculate_score(session["player_cards"])
        caption = f"ð Split Hand 2/2 | Bet: {session['bet']} â­ | Score: {hand2_score}"
        msg = await bj_send_table(context, session, hide_bot_second=True, reply_markup=reply_markup, caption=caption)
        session["message_id"] = msg.message_id
    else:
        # Both hands done — bot plays, then resolve each hand
        session["state"] = "bot_turn"

        # Bot draws (using combined view of last hand for the image)
        await bj_send_table(context, session, hide_bot_second=False,
                            caption="🤖 Bot's turn...")
        await asyncio.sleep(1)

        while bj_calculate_score(session["bot_cards"]) < 17:
            session["bot_cards"].append(session["deck"].pop())
            bot_score = bj_calculate_score(session["bot_cards"])
            await bj_send_table(context, session, hide_bot_second=False,
                                caption=f"🤖 Bot draws... ({bot_score})")
            await asyncio.sleep(1)

        bot_score = bj_calculate_score(session["bot_cards"])
        total_payout = 0
        results_text = []

        for hi in range(2):
            hand = session["split_hands"][hi]
            hand_score = bj_calculate_score(hand)
            hand_bet = session["split_bets"][hi]
            hand_str = bj_hand_str(hand)

            if hand_score > 21:
                results_text.append(f"Hand {hi+1}: {hand_str} = {hand_score} (Bust!) — Lost {hand_bet} ⭐")
            else:
                res_type, payout = bj_resolve(hand_score, bot_score, hand_bet)
                total_payout += payout
                if res_type == "win":
                    results_text.append(f"Hand {hi+1}: {hand_str} = {hand_score} — Won {payout} ⭐")
                elif res_type == "push":
                    results_text.append(f"Hand {hi+1}: {hand_str} = {hand_score} — Push (returned {payout} ⭐)")
                else:
                    results_text.append(f"Hand {hi+1}: {hand_str} = {hand_score} — Lost {hand_bet} ⭐")

        if total_payout > 0:
            adjust_user_balance(user_id, total_payout, game=True)
            user_balances[user_id] = get_user_balance(user_id)

        total_bet = session["split_bets"][0] + session["split_bets"][1]
        is_win = total_payout > total_bet
        update_game_stats(user_id, 'blackjack', total_bet, total_payout if is_win else 0, is_win)

        bot_hand = bj_hand_str(session["bot_cards"])
        result_lines = "\n".join(results_text)
        text = (
            f"ð <b>Split Results</b>\n\n"
            f"{result_lines}\n\n"
            f"Bot: {bot_hand} = {bot_score}\n\n"
            f"💰 Total payout: <b>{total_payout} ⭐</b>"
        )

        # Final image with last hand shown
        await bj_send_table(context, session, hide_bot_second=False, result_text="SPLIT RESULT")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(t("bj_play_again", user_id=user_id), callback_data="bj_play_again")]
        ])
        await context.bot.send_message(
            chat_id=session["chat_id"], text=text,
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )

        if user_id in blackjack_sessions:
            del blackjack_sessions[user_id]


async def bj_hand_complete(context, session, user_id, busted=False):
    """Called when current hand is done (stand or bust). Handles split routing."""
    if session.get("split_hand_index") is not None:
        if busted:
            session["split_results"][session["split_hand_index"]] = bj_calculate_score(session["player_cards"])
        await bj_advance_split(context, session, user_id)
    else:
        if busted:
            session["state"] = "finished"
            await bj_finish_game(context, session, user_id, "bust", 0)
        else:
            await bj_bot_turn(context, session, user_id)


async def bj_handle_hit(query, context, user_id):
    """Handle hit action."""
    session = blackjack_sessions.get(user_id)
    if not session or session["state"] != "playing":
        await query.answer(t("bj_no_active", user_id=user_id), show_alert=True)
        return

    await query.answer()
    session["player_cards"].append(session["deck"].pop())
    player_score = bj_calculate_score(session["player_cards"])

    if player_score > 21:
        await bj_hand_complete(context, session, user_id, busted=True)
        return

    if player_score == 21:
        await bj_hand_complete(context, session, user_id, busted=False)
        return

    # Continue playing
    reply_markup = bj_action_buttons(session, user_id)
    hand_label = ""
    if session.get("split_hand_index") is not None:
        hand_label = f"Hand {session['split_hand_index']+1}/2 | "
    caption = f"ð {hand_label}Bet: {session['bet']} â­ | Score: {player_score}"
    await bj_send_table(context, session, hide_bot_second=True, reply_markup=reply_markup, caption=caption)


async def bj_handle_stand(query, context, user_id):
    """Handle stand action."""
    session = blackjack_sessions.get(user_id)
    if not session or session["state"] != "playing":
        await query.answer(t("bj_no_active", user_id=user_id), show_alert=True)
        return

    await query.answer()
    await bj_hand_complete(context, session, user_id, busted=False)


async def bj_handle_double(query, context, user_id):
    """Handle double down: double bet, deal one card, auto-stand."""
    session = blackjack_sessions.get(user_id)
    if not session or session["state"] != "playing":
        await query.answer(t("bj_no_active", user_id=user_id), show_alert=True)
        return

    if len(session["player_cards"]) != 2:
        await query.answer(t("bj_double_first_only", user_id=user_id), show_alert=True)
        return

    bet = session["bet"]
    balance = get_user_balance(user_id)
    if balance < bet:
        await query.answer(t("bj_insufficient_double", user_id=user_id), show_alert=True)
        return

    await query.answer()

    # Deduct additional bet
    adjust_user_balance(user_id, -bet, game=True)
    user_balances[user_id] = get_user_balance(user_id)
    session["bet"] = bet * 2
    # Update split bet if applicable
    if session.get("split_hand_index") is not None:
        session["split_bets"][session["split_hand_index"]] = bet * 2

    # Deal exactly one card
    session["player_cards"].append(session["deck"].pop())
    player_score = bj_calculate_score(session["player_cards"])

    if player_score > 21:
        await bj_hand_complete(context, session, user_id, busted=True)
        return

    # Auto-stand after double
    await bj_hand_complete(context, session, user_id, busted=False)


async def bj_handle_split(query, context, user_id):
    """Handle split: split pair into two hands played sequentially."""
    session = blackjack_sessions.get(user_id)
    if not session or session["state"] != "playing":
        await query.answer(t("bj_no_active", user_id=user_id), show_alert=True)
        return

    cards = session["player_cards"]
    if len(cards) != 2 or cards[0]["value"] != cards[1]["value"]:
        await query.answer(t("bj_split_pair_only", user_id=user_id), show_alert=True)
        return

    bet = session["bet"]
    balance = get_user_balance(user_id)
    if balance < bet:
        await query.answer(t("bj_insufficient_split", user_id=user_id), show_alert=True)
        return

    await query.answer()

    # Deduct additional bet for second hand
    adjust_user_balance(user_id, -bet, game=True)
    user_balances[user_id] = get_user_balance(user_id)

    # Create two hands
    card1, card2 = cards[0], cards[1]
    hand1 = [card1, session["deck"].pop()]
    hand2 = [card2, session["deck"].pop()]

    session["split_hands"] = [hand1, hand2]
    session["split_bets"] = [bet, bet]
    session["split_results"] = [None, None]
    session["split_hand_index"] = 0
    session["player_cards"] = hand1  # Play first hand
    session["original_bet"] = bet

    # Show first hand
    reply_markup = bj_action_buttons(session, user_id)
    score = bj_calculate_score(hand1)
    caption = f"ð Split Hand 1/2 | Bet: {bet} â­ | Score: {score}"
    await bj_send_table(context, session, hide_bot_second=True, reply_markup=reply_markup, caption=caption)


async def bj_handle_forfeit(query, context, user_id):
    """Handle forfeit: return half the bet."""
    session = blackjack_sessions.get(user_id)
    if not session or session["state"] != "playing":
        await query.answer(t("bj_no_active", user_id=user_id), show_alert=True)
        return

    await query.answer()
    session["state"] = "finished"
    half_bet = session["bet"] // 2
    await bj_finish_game(context, session, user_id, "forfeit", half_bet)


async def handle_blackjack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all bj_ callbacks."""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # Play again
    if data == "bj_play_again":
        await query.answer()
        # Simulate /bj command
        balance = get_user_balance(user_id)
        if balance <= 0:
            await query.answer(t("insufficient_balance", user_id=user_id), show_alert=True)
            return
        if user_id in blackjack_sessions:
            await query.answer(t("bj_game_active", user_id=user_id), show_alert=True)
            return
        keyboard = [
            [
                InlineKeyboardButton("50 ⭐", callback_data="bj_bet_50"),
                InlineKeyboardButton("100 ⭐", callback_data="bj_bet_100"),
                InlineKeyboardButton("250 ⭐", callback_data="bj_bet_250"),
            ],
            [
                InlineKeyboardButton("500 ⭐", callback_data="bj_bet_500"),
                InlineKeyboardButton("1000 ⭐", callback_data="bj_bet_1000"),
                InlineKeyboardButton(t("bj_custom_btn", user_id=user_id), callback_data="bj_bet_custom"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = f"ð <b>{t('bj_title', user_id=user_id).replace('ð ', '')}</b>\n\n{t('bj_select_bet', user_id=user_id)}\n\n{t('bj_balance_label', user_id=user_id)}: <b>{balance} â­</b>"
        sent = await context.bot.send_message(
            chat_id=query.message.chat_id, text=text,
            parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
        register_menu_owner(sent, user_id)
        return

    # Bet selection
    if data.startswith("bj_bet_"):
        bet_str = data.replace("bj_bet_", "")

        if bet_str == "custom":
            await query.answer()
            context.user_data["bj_custom_bet_pending"] = {
                "chat_id": query.message.chat_id,
                "message_id": query.message.message_id,
            }
            await query.edit_message_text(
                f"ð <b>Blackjack - Custom Bet</b>\n\n"
                f"💰 Type your bet amount in stars (e.g. <code>150</code>)\n"
                f"Minimum: 10 ⭐",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            bet = int(bet_str)
        except ValueError:
            await query.answer()
            return

        if bet < 10:
            await query.answer(t("bj_min_bet_alert", user_id=user_id), show_alert=True)
            return

        balance = get_user_balance(user_id)
        if balance < bet:
            await query.answer(t("err_insuf_bal_alert", user_id=user_id), show_alert=True)
            return

        if user_id in blackjack_sessions:
            await query.answer(t("bj_game_active", user_id=user_id), show_alert=True)
            return

        await query.answer()
        # Delete the bet menu message
        try:
            await query.message.delete()
        except Exception:
            pass
        await bj_start_game(context, update, user_id, bet)
        return

    # Session-based actions: verify ownership
    session = blackjack_sessions.get(user_id)
    action_callbacks = ("bj_hit", "bj_stand", "bj_double", "bj_split", "bj_forfeit")
    if data in action_callbacks and not session:
        for uid, s in blackjack_sessions.items():
            if (s.get("chat_id") == query.message.chat_id
                    and s.get("message_id") == query.message.message_id
                    and uid != user_id):
                await query.answer(t("err_not_your_game", user_id=user_id), show_alert=True)
                return
        await query.answer(t("bj_no_active", user_id=user_id), show_alert=True)
        return

    if data in action_callbacks and session:
        if session.get("state") != "playing":
            await query.answer(t("alert_please_wait", user_id=user_id), show_alert=True)
            return

    # Route game actions
    if data == "bj_hit":
        await bj_handle_hit(query, context, user_id)
    elif data == "bj_stand":
        await bj_handle_stand(query, context, user_id)
    elif data == "bj_double":
        await bj_handle_double(query, context, user_id)
    elif data == "bj_split":
        await bj_handle_split(query, context, user_id)
    elif data == "bj_forfeit":
        await bj_handle_forfeit(query, context, user_id)


@handle_errors
async def handle_game_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle dice emoji messages from player during their turn"""
    user_id = update.effective_user.id

    if is_banned(user_id):
        return

    if is_frozen(user_id) and not is_admin(user_id):
        return

    session = game_sessions.get(user_id)
    if not session or not session.get('waiting_for_player'):
        return

    chat_id = update.effective_chat.id
    game_type = session['game_type']
    config = GAME_CONFIG[game_type]

    dice = update.message.dice
    if dice.emoji != config['tg_emoji']:
        return

    session['player_rolls_done'] += 1
    session['player_total'] += dice.value

    # Double mode needs 2 rolls from the player
    if session['player_rolls_done'] < session['player_rolls_needed']:
        return

    # All player rolls received — now bot rolls
    session['waiting_for_player'] = False

    mode = session['mode']
    if mode == "double":
        b1 = await context.bot.send_dice(chat_id=chat_id, emoji=config['tg_emoji'])
        await asyncio.sleep(2)
        b2 = await context.bot.send_dice(chat_id=chat_id, emoji=config['tg_emoji'])
        await asyncio.sleep(2)
        session['bot_value'] = b1.dice.value + b2.dice.value
    else:
        bot_msg = await context.bot.send_dice(chat_id=chat_id, emoji=config['tg_emoji'])
        await asyncio.sleep(2)
        session['bot_value'] = bot_msg.dice.value

    await complete_round(context, chat_id, user_id)


# Template functions
import sqlite3

def init_templates_db():
    """Initialize the templates database"""
    conn = sqlite3.connect(TEMPLATES_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS templates
                 (command_name TEXT PRIMARY KEY,
                  html_content TEXT,
                  entities TEXT,
                  reply_markup TEXT)''')
    conn.commit()
    conn.close()

def save_template(command_name, html_content, entities=None, reply_markup=None):
    """Save a template for a command"""
    try:
        init_templates_db()
        conn = sqlite3.connect(TEMPLATES_DB)
        c = conn.cursor()
        
        # Serialize entities and reply_markup to JSON
        entities_json = json.dumps(entities) if entities else None
        reply_markup_json = json.dumps(reply_markup) if reply_markup else None
        
        c.execute('''INSERT OR REPLACE INTO templates 
                     (command_name, html_content, entities, reply_markup)
                     VALUES (?, ?, ?, ?)''',
                  (command_name, html_content, entities_json, reply_markup_json))
        conn.commit()
        conn.close()
        logger.info(f"Template saved for command: /{command_name} - text length: {len(html_content)}, entities: {len(entities) if entities else 0}")
    except Exception as e:
        logger.error(f"Error saving template for /{command_name}: {e}", exc_info=True)
        raise

def get_template(command_name):
    """Get a template for a command"""
    try:
        init_templates_db()
        conn = sqlite3.connect(TEMPLATES_DB)
        c = conn.cursor()
        
        c.execute('SELECT html_content, entities, reply_markup FROM templates WHERE command_name = ?',
                  (command_name,))
        result = c.fetchone()
        conn.close()
        
        if result:
            html_content, entities_json, reply_markup_json = result
            entities = json.loads(entities_json) if entities_json else None
            reply_markup = json.loads(reply_markup_json) if reply_markup_json else None
            logger.info(f"Template retrieved for /{command_name}: text length={len(html_content) if html_content else 0}, entities={len(entities) if entities else 0}")
            return html_content, entities, reply_markup
        else:
            logger.debug(f"No template found in database for /{command_name}")
        return None, None, None
    except Exception as e:
        logger.error(f"Error retrieving template for /{command_name}: {e}", exc_info=True)
        return None, None, None

def replace_template_variables(template_html, user_id, **kwargs):
    """Replace variables in template HTML"""
    balance = get_user_balance(user_id)
    balance_usd = balance * STARS_TO_USD
    profile = user_profiles.get(user_id, {})
    username = profile.get('username', '')
    display_name = profile.get('display_name', '')
    
    # Default replacements
    replacements = {
        '{amount}': str(kwargs.get('amount', '')),
        '{balance}': f"{balance:,.0f}",
        '{balance_usd}': f"${balance_usd:.2f}",
        '{username}': username or display_name or f"User_{user_id}",
        '{user_id}': str(user_id)
    }
    
    # Add any additional kwargs
    for key, value in kwargs.items():
        if key not in ['amount', 'balance', 'username']:
            replacements[f'{{{key}}}'] = str(value)
    
    result = template_html
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    
    return result


# ==================== EMOJI CUSTOMIZATION DB & HELPERS ====================

# Regex that captures individual Unicode emojis (single codepoint or multi-codepoint sequences)
_EMOJI_RE = re.compile(
    "(?:"
    "[\U0001F600-\U0001F64F]"  # emoticons
    "|[\U0001F300-\U0001F5FF]"  # symbols & pictographs
    "|[\U0001F680-\U0001F6FF]"  # transport & map
    "|[\U0001F1E0-\U0001F1FF]"  # flags
    "|[\U00002702-\U000027B0]"  # dingbats
    "|[\U0000FE00-\U0000FE0F]"  # variation selectors
    "|[\U0001F900-\U0001F9FF]"  # supplemental symbols
    "|[\U0001FA00-\U0001FA6F]"  # chess symbols
    "|[\U0001FA70-\U0001FAFF]"  # symbols extended-A
    "|[\U00002600-\U000026FF]"  # misc symbols
    "|[\U00002300-\U000023FF]"  # misc technical
    "|[\U0000200D]"             # ZWJ
    "|[\U000024C2-\U0001F251]"  # enclosed characters
    ")+"
)


def init_emoji_db():
    """Create global emoji_mappings table: normal_emoji PRIMARY KEY, custom_emoji_id. No user_id/message_key."""
    conn = sqlite3.connect(EMOJI_DB)
    c = conn.cursor()
    # Check for old schema (message_key column) and migrate
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='emoji_mappings'")
    if c.fetchone():
        try:
            c.execute("PRAGMA table_info(emoji_mappings)")
            cols = [row[1] for row in c.fetchall()]
            if "message_key" in cols:
                # Old schema: migrate to new global table
                c.execute('''CREATE TABLE IF NOT EXISTS emoji_mappings_new (
                    normal_emoji TEXT PRIMARY KEY,
                    custom_emoji_id TEXT NOT NULL
                )''')
                c.execute('''INSERT OR REPLACE INTO emoji_mappings_new (normal_emoji, custom_emoji_id)
                             SELECT normal_emoji, custom_emoji_id FROM emoji_mappings''')
                c.execute('DROP TABLE emoji_mappings')
                c.execute('ALTER TABLE emoji_mappings_new RENAME TO emoji_mappings')
                conn.commit()
        except Exception as e:
            logger.warning(f"Emoji migration: {e}")
    c.execute('''CREATE TABLE IF NOT EXISTS emoji_mappings (
        normal_emoji TEXT PRIMARY KEY,
        custom_emoji_id TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()


def load_global_emoji_map():
    """Load all emoji mappings into memory. Call at startup and after any save."""
    global emoji_map
    init_emoji_db()
    conn = sqlite3.connect(EMOJI_DB)
    c = conn.cursor()
    try:
        c.execute('SELECT normal_emoji, custom_emoji_id FROM emoji_mappings')
        emoji_map = {row[0]: row[1] for row in c.fetchall()}
    except sqlite3.OperationalError:
        emoji_map = {}
    conn.close()
    logger.info(f"Loaded {len(emoji_map)} global emoji mappings.")


def save_global_emoji_mapping(normal_emoji: str, custom_emoji_id: str):
    """Save one global mapping and update in-memory cache."""
    init_emoji_db()
    conn = sqlite3.connect(EMOJI_DB)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO emoji_mappings (normal_emoji, custom_emoji_id) VALUES (?, ?)''',
              (normal_emoji, custom_emoji_id))
    conn.commit()
    conn.close()
    emoji_map[normal_emoji] = custom_emoji_id
    logger.info(f"Global emoji saved: {normal_emoji} -> {custom_emoji_id}")


def seed_emoji_map_from_packs():
    """Bulk-insert all 126 emoji IDs from the two Housebalcasino packs using INSERT OR IGNORE
    so manually-set overrides (via /emoji command) always take precedence."""
    init_emoji_db()
    conn = sqlite3.connect(EMOJI_DB)
    c = conn.cursor()
    c.executemany(
        "INSERT OR IGNORE INTO emoji_mappings (normal_emoji, custom_emoji_id) VALUES (?, ?)",
        list(PACK_EMOJI_MAP.items()),
    )
    conn.commit()
    conn.close()
    # Merge into in-memory map without overwriting any existing entries
    for em, cid in PACK_EMOJI_MAP.items():
        if em not in emoji_map:
            emoji_map[em] = cid
    logger.info(f"Seeded {len(PACK_EMOJI_MAP)} pack emoji mappings (INSERT OR IGNORE).")


def extract_emojis_ordered(text: str) -> list:
    """Extract all normal emojis from text, preserving order and duplicates.
    Returns: [("emoji_char", char_index_in_text), ...]
    """
    results = []
    for match in _EMOJI_RE.finditer(text):
        results.append((match.group(), match.start()))
    return results


def track_bot_message(chat_id: int, message_key: str, text: str, message_id: int):
    """Track the last bot message in chat for /emoji (extract emojis to map)."""
    last_bot_messages[chat_id] = {
        "message_key": message_key,
        "text": text,
        "message_id": message_id
    }


def apply_global_emoji_replace(text: str) -> str:
    """Replace every normal emoji that has a global mapping with <tg-emoji> HTML. Used before sending any message."""
    if not text or not emoji_map:
        return text
    import html as html_mod
    result = text
    # Work backwards so offsets stay valid
    for match in list(_EMOJI_RE.finditer(text))[::-1]:
        emoji_char = match.group()
        start, end = match.span()
        if emoji_char in emoji_map:
            custom_id = emoji_map[emoji_char]
            replacement = f'<tg-emoji emoji-id="{custom_id}">{html_mod.escape(emoji_char)}</tg-emoji>'
            result = result[:start] + replacement + result[end:]
    return result


# ── Button style/icon upgrade (Bot API 9.4: icon_custom_emoji_id + style) ──────
# Three valid style values.  "warning" and "secondary" don't exist in the API;
# buttons that map to those just get no style tag (default appearance).
_BTN_DANGER  = {"❌", "✖", "cancel", "close", "reject", "ban", "delete", "remove", "no"}
_BTN_SUCCESS = {"✅", "deposit", "confirm", "add", "buy", "pay", "yes", "approve", "accept", "bonus", "redeem", "claim"}
_BTN_PRIMARY = {"🎲", "🎰", "🎯", "🏆", "🎳", "🏀", "⚽", "🎱", "🎮",
                "play", "game", "spin", "bet", "start", "leaderboard", "dice",
                "darts", "bowling", "football", "basket", "coinflip", "blackjack",
                "slots", "mines", "predict"}


def _detect_button_attrs(text: str) -> tuple[str | None, str | None]:
    """Return (style, icon_custom_emoji_id) for a button label.

    style is one of "primary" | "success" | "danger" | None.
    icon_custom_emoji_id is the pack ID of the first mapped emoji in the label,
    or None if none found.
    """
    low = text.lower()

    style: str | None = None
    if any(sig in text or sig in low for sig in _BTN_DANGER):
        style = "danger"
    elif any(sig in text or sig in low for sig in _BTN_SUCCESS):
        style = "success"
    elif any(sig in text or sig in low for sig in _BTN_PRIMARY):
        style = "primary"

    # Icon: first emoji in the text that has a pack mapping
    icon_id: str | None = None
    if emoji_map:
        for match in _EMOJI_RE.finditer(text):
            em = match.group()
            if em in emoji_map:
                icon_id = emoji_map[em]
                break

    return style, icon_id


def _upgrade_button(btn: InlineKeyboardButton) -> InlineKeyboardButton:
    """Return a copy of btn with style + icon_custom_emoji_id injected via api_kwargs."""
    style, icon_id = _detect_button_attrs(btn.text)
    if not style and not icon_id:
        return btn
    extra: dict = {}
    if style:
        extra["style"] = style
    if icon_id:
        extra["icon_custom_emoji_id"] = icon_id
    existing = dict(btn.api_kwargs) if btn.api_kwargs else {}
    merged = {**extra, **existing}  # existing explicit api_kwargs always win
    return InlineKeyboardButton(
        text=btn.text,
        url=btn.url,
        callback_data=btn.callback_data,
        switch_inline_query=btn.switch_inline_query,
        switch_inline_query_current_chat=btn.switch_inline_query_current_chat,
        callback_game=btn.callback_game,
        pay=btn.pay,
        login_url=btn.login_url,
        web_app=btn.web_app,
        switch_inline_query_chosen_chat=btn.switch_inline_query_chosen_chat,
        copy_text=btn.copy_text,
        api_kwargs=merged,
    )


def _upgrade_keyboard(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Walk every button in an InlineKeyboardMarkup and apply style/icon upgrades."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    return InlineKeyboardMarkup(
        [[_upgrade_button(btn) for btn in row] for row in markup.inline_keyboard]
    )


class EmojiAwareBot(Bot):
    """Bot that applies global emoji replacement to all sent/edited text and captions,
    and upgrades inline keyboard buttons with Bot API 9.4 style + icon fields."""

    @staticmethod
    def _patch_kwargs(kwargs: dict) -> dict:
        """Apply emoji replacement to text/caption and button upgrades to reply_markup."""
        text = kwargs.get("text")
        if text:
            kwargs = {**kwargs, "text": apply_global_emoji_replace(text)}
        caption = kwargs.get("caption")
        if caption:
            kwargs = {**kwargs, "caption": apply_global_emoji_replace(caption)}
        markup = kwargs.get("reply_markup")
        if isinstance(markup, InlineKeyboardMarkup):
            kwargs = {**kwargs, "reply_markup": _upgrade_keyboard(markup)}
        return kwargs

    async def send_message(self, *args, **kwargs):
        return await super().send_message(*args, **self._patch_kwargs(kwargs))

    async def edit_message_text(self, *args, **kwargs):
        return await super().edit_message_text(*args, **self._patch_kwargs(kwargs))

    async def edit_message_caption(self, *args, **kwargs):
        return await super().edit_message_caption(*args, **self._patch_kwargs(kwargs))


# ==================== /emoji COMMAND & FLOW ====================

@handle_errors
async def emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: Start custom emoji replacement flow for the last bot message in this chat."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_admin(user_id):
        await update.message.reply_html(t("emoji_admin_only", user_id=user_id))
        return

    # Get last bot message for this chat
    last = last_bot_messages.get(chat_id)
    if not last:
        await update.message.reply_html(
            "❌ <b>No tracked bot message found in this chat.</b>\n\n"
            "Send a command first (e.g. /start, /balance), then use /emoji to customise its emojis."
        )
        return

    text = last["text"]
    all_emojis = extract_emojis_ordered(text)
    if not all_emojis:
        await update.message.reply_html(t("emoji_no_emojis", user_id=user_id))
        return

    # Only ask for emojis NOT already in global map (no re-asking)
    emojis_to_ask = [(em, pos) for em, pos in all_emojis if em not in emoji_map]
    if not emojis_to_ask:
        await update.message.reply_html(
            "✅ <b>All emojis in the last message are already mapped.</b> No new custom emojis to set."
        )
        return

    emoji_replace_flow[user_id] = {
        "chat_id": chat_id,
        "emojis": emojis_to_ask,
        "current_index": 0,
        "total": len(emojis_to_ask),
    }

    preview_lines = [f"  {i + 1}. {em}" for i, (em, _) in enumerate(emojis_to_ask)]
    preview = "\n".join(preview_lines)
    first_emoji = emojis_to_ask[0][0]

    await update.message.reply_html(
        f"🎯 <b>Global Emoji</b>\n\n"
        f"<b>{len(emojis_to_ask)}</b> emoji(s) not yet saved:\n{preview}\n\n"
        f"¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â¢â\n"
        f"Send a <b>custom emoji</b> to replace #1 ({first_emoji}). /skip to keep · /cancel to abort."
    )


async def handle_emoji_flow_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Process incoming message during emoji replacement flow.
    Returns True if message was consumed, False otherwise.
    """
    user_id = update.effective_user.id
    if user_id not in emoji_replace_flow:
        return False

    flow = emoji_replace_flow[user_id]
    message = update.message
    text = (message.text or "").strip()

    # Handle /skip
    if text.lower() == "/skip":
        idx = flow["current_index"]
        emoji_char = flow["emojis"][idx][0]
        logger.info(f"Admin {user_id} skipped emoji #{idx + 1} ({emoji_char})")

        flow["current_index"] += 1
        return await _advance_emoji_flow(update, context, user_id)

    # Handle /cancel
    if text.lower() == "/cancel":
        del emoji_replace_flow[user_id]
        await message.reply_html(t("emoji_cancelled", user_id=user_id))
        return True

    # Look for custom_emoji_id in message entities
    custom_emoji_id = None
    if message.entities:
        for entity in message.entities:
            etype = entity.type.name if hasattr(entity.type, 'name') else str(entity.type)
            if etype == "CUSTOM_EMOJI" and hasattr(entity, 'custom_emoji_id') and entity.custom_emoji_id:
                custom_emoji_id = str(entity.custom_emoji_id)
                break

    if not custom_emoji_id:
        await message.reply_html(
            "âš  <b>No custom emoji detected.</b>\n"
            "Please send a <b>premium/custom emoji</b>, /skip to keep original, or /cancel to abort."
        )
        return True

    idx = flow["current_index"]
    emoji_char = flow["emojis"][idx][0]
    save_global_emoji_mapping(emoji_char, custom_emoji_id)

    await message.reply_html(
        f"✅ Emoji <b>#{idx + 1}</b> ({emoji_char}) → custom <code>{custom_emoji_id}</code>"
    )

    flow["current_index"] += 1
    return await _advance_emoji_flow(update, context, user_id)


async def _advance_emoji_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Advance to the next emoji or finish the flow."""
    flow = emoji_replace_flow[user_id]
    idx = flow["current_index"]
    total = flow["total"]

    if idx >= total:
        del emoji_replace_flow[user_id]
        await update.message.reply_html(
            f"✅ <b>Global emoji customization complete!</b>\n\n"
            f"Saved mappings apply to <b>all users</b> and all messages."
        )
        return True

    # Ask for the next emoji
    emoji_char = flow["emojis"][idx][0]
    await update.message.reply_html(
        f"Send a <b>custom emoji</b> for position <b>#{idx + 1}</b> of {total} ({emoji_char})\n\n"
        f"/skip to keep original · /cancel to abort"
    )
    return True


async def send_bot_reply_html(message_obj, text: str, message_key: str = None,
                              reply_markup=None, chat_id: int = None, **kwargs):
    """Send an HTML reply with global emoji replace + optional tracking for /emoji."""
    send_text = apply_global_emoji_replace(text)

    if hasattr(message_obj, 'reply_html'):
        sent = await message_obj.reply_html(send_text, reply_markup=reply_markup, **kwargs)
    elif hasattr(message_obj, 'reply_text'):
        sent = await message_obj.reply_text(send_text, parse_mode=ParseMode.HTML,
                                            reply_markup=reply_markup, **kwargs)
    else:
        sent = None

    if sent and message_key:
        cid = chat_id or (sent.chat.id if sent else None)
        if cid:
            track_bot_message(cid, message_key, text, sent.message_id)
    return sent


async def send_template_message(update_or_message, context, command_name, user_id, **kwargs):
    """Send a message using a template if available, otherwise use default"""
    from telegram import MessageEntity
    import re
    from html import unescape
    import html
    
    try:
        # Try to get template
        template_html, template_entities, template_reply_markup = get_template(command_name)
        
        if not template_html:
            logger.debug(f"No template found for /{command_name}")
            return None
        
        logger.info(f"Template found for /{command_name}")
        
        if template_html:
            logger.info(f"Template found for /{command_name}, processing...")
            # Template is saved as plain text, so use it directly
            template_plain = template_html  # It's already plain text
            
            # Replace variables in template (global emoji replace is applied by EmojiAwareBot when sending)
            message_text = replace_template_variables(template_plain, user_id, **kwargs)
            
            logger.info(f"Template text length: {len(template_plain)}, Message text length: {len(message_text)}")
            logger.info(f"Template entities count: {len(template_entities) if template_entities else 0}")
            if template_entities:
                logger.info(f"First entity: {template_entities[0] if template_entities else 'None'}")
            
            # Reconstruct entities with custom emojis
            # Need to recalculate offsets after variable replacement
            entities_list = []
            if template_entities:
                # First, find emoji positions in the original template (plain text)
                emoji_pattern = re.compile(
                    "["
                    "\U0001F600-\U0001F64F"
                    "\U0001F300-\U0001F5FF"
                    "\U0001F680-\U0001F6FF"
                    "\U0001F1E0-\U0001F1FF"
                    "\U00002702-\U000027B0"
                    "\U000024C2-\U0001F251"
                    "\U0001F900-\U0001F9FF"
                    "\U0001FA00-\U0001FA6F"
                    "\U0001FA70-\U0001FAFF"
                    "]+"
                )
                
                # Create a mapping of emoji -> custom_emoji_id from saved entities
                emoji_to_custom_id = {}
                for entity_dict in template_entities:
                    if entity_dict.get("type") == "CUSTOM_EMOJI":
                        # Find which emoji this entity refers to in original template
                        orig_offset = entity_dict.get("offset", 0)
                        orig_length = entity_dict.get("length", 0)
                        custom_emoji_id = entity_dict.get("custom_emoji_id")
                        
                        if orig_offset < len(template_plain) and custom_emoji_id:
                            emoji_in_template = template_plain[orig_offset:orig_offset + orig_length]
                            if emoji_in_template:
                                emoji_to_custom_id[emoji_in_template] = custom_emoji_id
                                logger.info(f"Mapped emoji '{emoji_in_template}' (offset {orig_offset}) to custom_emoji_id {custom_emoji_id}")
                
                logger.info(f"Created emoji mapping with {len(emoji_to_custom_id)} entries")
                
                # Now find emojis in the new message text and create entities
                matches = list(emoji_pattern.finditer(message_text))
                logger.debug(f"Found {len(matches)} emoji matches in message text")
                logger.debug(f"Emoji mapping has {len(emoji_to_custom_id)} entries: {list(emoji_to_custom_id.keys())}")
                
                for match in reversed(matches):
                    emoji = match.group()
                    start = match.start()
                    length = len(emoji)
                    
                    # Check if this emoji has a custom emoji version
                    if emoji in emoji_to_custom_id:
                        custom_emoji_id = emoji_to_custom_id[emoji]
                        try:
                            # Ensure custom_emoji_id is correct type
                            if isinstance(custom_emoji_id, str):
                                try:
                                    custom_emoji_id = int(custom_emoji_id)
                                except (ValueError, TypeError):
                                    pass
                            
                            entity = MessageEntity(
                                MessageEntity.CUSTOM_EMOJI,
                                start,
                                length,
                                custom_emoji_id=custom_emoji_id
                            )
                            entities_list.append(entity)
                            logger.debug(f"Created entity for emoji {emoji} at offset {start} with custom_emoji_id {custom_emoji_id}")
                        except Exception as e:
                            logger.error(f"Error creating entity for emoji {emoji}: {e}")
                            continue
                    else:
                        logger.debug(f"Emoji {emoji} not found in mapping")
                
                logger.info(f"Created {len(entities_list)} entities for custom emojis")
            
            # Sort entities by offset
            entities_list.sort(key=lambda e: e.offset)
            
            # Reconstruct reply_markup if present
            reply_markup = None
            if template_reply_markup:
                keyboard = []
                for row in template_reply_markup:
                    button_row = []
                    for button_dict in row:
                        text = button_dict.get("text", "")
                        if button_dict.get("callback_data"):
                            button_row.append(InlineKeyboardButton(text, callback_data=button_dict["callback_data"]))
                        elif button_dict.get("url"):
                            button_row.append(InlineKeyboardButton(text, url=button_dict["url"]))
                        else:
                            button_row.append(InlineKeyboardButton(text))
                    keyboard.append(button_row)
                reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            
            # Send message with entities
            if entities_list:
                logger.info(f"Sending message with {len(entities_list)} custom emoji entities")
                # Use reply_text with entities parameter (parse_mode=None when using entities)
                if hasattr(update_or_message, 'reply_text'):
                    sent_msg = await update_or_message.reply_text(
                        message_text,
                        entities=entities_list,
                        reply_markup=reply_markup
                    )
                    logger.info(f"Message sent successfully with custom emojis")
                    # Track for /emoji
                    if sent_msg:
                        cid = sent_msg.chat.id if sent_msg.chat else None
                        if cid:
                            track_bot_message(cid, command_name, message_text, sent_msg.message_id)
                    return sent_msg
                else:
                    # Fallback to HTML without entities
                    logger.warning("Cannot use reply_text, falling back to HTML without entities")
                    return await update_or_message.reply_html(
                        message_text,
                        reply_markup=reply_markup
                    )
            else:
                # Send message (EmojiAwareBot applies global emoji replace)
                if hasattr(update_or_message, 'reply_html'):
                    sent = await update_or_message.reply_html(message_text, reply_markup=reply_markup)
                else:
                    sent = await update_or_message.reply_text(message_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
                if sent:
                    track_bot_message(sent.chat.id, command_name, message_text, sent.message_id)
                return sent
        
        # No template found, return None to use default message
        return None
    except Exception as e:
        logger.error(f"Error in send_template_message: {e}", exc_info=True)
        # Return None to fall back to default message
        return None


def get_command_message_preview(command_name, user_id):
    """Get the current message text for a command (for template preview)"""
    try:
        if command_name == "start":
            balance = get_user_balance(user_id)
            balance_usd = balance * STARS_TO_USD
            profile = user_profiles.get(user_id, {})
            turnover = profile.get('total_bets', 0.0) * STARS_TO_USD
            admin_badge = " 👑" if is_admin(user_id) else ""
            bot_name = bot_identity.get("name", "Iibrate")
            channel_link = bot_identity.get("channel_link", "https://t.me/Iibrate")
            chat_link = bot_identity.get("chat_link", "https://t.me/librateds")
            support_username = bot_identity.get("support_username", "Iibratesupport")
            if support_username.startswith('@'):
                support_link = f"https://t.me/{support_username[1:]}"
            else:
                support_link = f"https://t.me/{support_username}"
            return t("welcome", user_id=user_id,
                bot_name=bot_name, admin_badge=admin_badge,
                balance_usd=balance_usd, turnover=turnover,
                channel_link=channel_link, chat_link=chat_link, support_link=support_link
            )
        elif command_name == "deposit" or command_name == "depo":
            return t("select_deposit", user_id=user_id)
        elif command_name == "balance" or command_name == "bal":
            balance = get_user_balance(user_id)
            balance_usd = balance * STARS_TO_USD
            admin_note = " (Admin - Unlimited)" if is_admin(user_id) else ""
            return t("your_balance", user_id=user_id, admin_note=admin_note, balance=balance, balance_usd=balance_usd)
        elif command_name == "help":
            return t("help_text", user_id=user_id) or t("available_commands", user_id=user_id)
        elif command_name == "gift":
            return get_random_gift_message()
        else:
            return f"Current message for /{command_name} command"
    except Exception as e:
        logger.error(f"Error getting command preview for {command_name}: {e}")
        return f"Error: Could not get preview for /{command_name}"

@handle_errors
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = update.effective_user
    
    # Auto-detect and set language on any message (if not already set)
    if user_id not in user_languages:
        user_lang_code = getattr(user, 'language_code', None) or ""
        detected = detect_lang(user_lang_code)
        user_languages[user_id] = detected
        db.set_user_language(user_id, detected)
    
    # Check if user is banned (allow admins and special flows)
    if is_banned(user_id) and not is_admin(user_id):
        # Allow admin flows even if admin is somehow banned (shouldn't happen)
        if not context.user_data.get('steal_state') and not context.user_data.get('waiting_for_bankroll') and not context.user_data.get('waiting_for_min_withdrawal'):
            return  # Silently ignore banned users
    
    text = (update.message.text or "").strip()
    
    # Handle emoji replacement flow (admin only) — must be checked before other handlers
    if user_id in emoji_replace_flow:
        consumed = await handle_emoji_flow_input(update, context)
        if consumed:
            return

    # Handle template setup mode (admin only)
    if user_id in template_setup_mode and template_setup_mode[user_id].get("active"):
        setup_state = template_setup_mode[user_id]
        
        # Check for /done or /cancel
        text_lower = text.lower()
        if text_lower == "/done":
            template_setup_mode[user_id] = {"active": False}
            await update.message.reply_html(t("emoji_template_exit", user_id=user_id))
            return
        if text_lower == "/cancel":
            template_setup_mode[user_id] = {"active": False}
            await update.message.reply_html(t("emoji_template_cancelled", user_id=user_id))
            return
        
        # If waiting for command name
        if setup_state.get("waiting_for_command"):
            command_name = text.strip().lower().replace("/", "")
            if not command_name:
                await update.message.reply_html(t("emoji_invalid_command", user_id=user_id))
                return
            
            # Get current message for this command (for preview)
            current_message = get_command_message_preview(command_name, user_id)
            
            # Send the current message to admin and ask for new template
            await update.message.reply_html(
                f"📋 <b>Current message for /{command_name}:</b>\n\n"
                f"{current_message}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ Now send the <b>message with emojis & variables</b> (e.g., \"Welcome {{username}}! 🎯✅\").\n\n"
                f"You can include:\n"
                f"• Premium/custom emojis (preserved)\n"
                f"• Variables: <code>{{username}}</code>, <code>{{balance}}</code>, <code>{{amount}}</code>\n"
                f"• Inline buttons and links (optional)\n"
                f"• HTML formatting"
            )
            
            template_setup_mode[user_id] = {
                "active": True,
                "current_command": command_name,
                "waiting_for_command": False,
                "waiting_for_message": True
            }
            return
        
        # If waiting for message template (single step)
        if setup_state.get("waiting_for_message"):
            command_name = setup_state.get("current_command")
            if not command_name:
                await update.message.reply_html(t("emoji_no_command_set", user_id=user_id))
                template_setup_mode[user_id] = {"active": False}
                return
            
            # Capture message HTML, entities, and reply_markup (for inline buttons)
            message = update.message
            html_content = message.html_text if hasattr(message, 'html_text') else message.text or ""
            
            if not html_content:
                await update.message.reply_html(t("emoji_invalid_message", user_id=user_id))
                return
            
            # Get entities (for custom emojis and links)
            entities = []
            if message.entities:
                for entity in message.entities:
                    entity_dict = {
                        "type": entity.type.name if hasattr(entity.type, 'name') else str(entity.type),
                        "offset": entity.offset,
                        "length": entity.length
                    }
                    # Preserve custom_emoji_id if present
                    if hasattr(entity, 'custom_emoji_id'):
                        entity_dict["custom_emoji_id"] = entity.custom_emoji_id
                    # Preserve URL for text_link
                    entity_type_str = entity.type.name if hasattr(entity.type, 'name') else str(entity.type)
                    if entity_type_str == 'text_link' and hasattr(entity, 'url'):
                        entity_dict["url"] = entity.url
                    entities.append(entity_dict)
            
            # Get reply_markup (inline keyboard) if present
            reply_markup = None
            if message.reply_markup and hasattr(message.reply_markup, 'inline_keyboard'):
                reply_markup = []
                for row in message.reply_markup.inline_keyboard:
                    button_row = []
                    for button in row:
                        button_dict = {
                            "text": button.text
                        }
                        if hasattr(button, 'callback_data') and button.callback_data:
                            button_dict["callback_data"] = button.callback_data
                        if hasattr(button, 'url') and button.url:
                            button_dict["url"] = button.url
                        if hasattr(button, 'web_app') and button.web_app:
                            # Store web_app as string representation
                            button_dict["web_app"] = str(button.web_app.url) if hasattr(button.web_app, 'url') else str(button.web_app)
                        button_row.append(button_dict)
                    reply_markup.append(button_row)
            
            # Save template (upsert on duplicate)
            save_template(command_name, html_content, entities, reply_markup)
            
            await update.message.reply_html(
                f"✅ Template saved for <code>/{command_name}</code>!\n\n"
                "Send another command name to set another template, or /done to finish."
            )
            
            # Reset to wait for next command
            template_setup_mode[user_id] = {
                "active": True,
                "current_command": None,
                "waiting_for_command": True
            }
            return
    
    # Handle steal command flow
    if context.user_data.get('steal_state'):
        await handle_steal_flow(update, context)
        return
    
    # Handle bankroll input from admin prompt
    if context.user_data.get('waiting_for_bankroll'):
        if not is_admin(user_id):
            context.user_data['waiting_for_bankroll'] = False
            await update.message.reply_html(translate_text("❌ Only admins can set bankroll.", user_id=user_id))
            return
        try:
            amount = float(text)
            global casino_bankroll_usd
            casino_bankroll_usd = amount
            db.set_casino_bankroll(amount)
            context.user_data['waiting_for_bankroll'] = False
            await update.message.reply_html(
                translate_text(f"✅ Bankroll updated.\n\n🏦 Casino Bankroll\n💵 USD: ${casino_bankroll_usd:,.2f}", user_id=user_id)
            )
        except ValueError:
            await update.message.reply_html(translate_text("❌ Please enter a valid number (e.g., 2493.23).", user_id=user_id))
        return
    
    # Handle minimum withdrawal input (admin only)
    if context.user_data.get('waiting_for_min_withdrawal'):
        if not is_admin(user_id):
            context.user_data['waiting_for_min_withdrawal'] = False
            await update.message.reply_html(translate_text("❌ Only admins can set minimum withdrawal.", user_id=user_id))
            return
        try:
            amount = int(text)
            if amount < 1:
                await update.message.reply_html(translate_text("❌ Minimum withdrawal must be at least 1 ⭐", user_id=user_id))
                return
            global MIN_WITHDRAWAL
            MIN_WITHDRAWAL = amount
            db.set_min_withdrawal(amount)
            context.user_data['waiting_for_min_withdrawal'] = False
            await update.message.reply_html(
                f"✅ <b>Minimum withdrawal updated!</b>\n\n"
                f"💰 New minimum: <b>{MIN_WITHDRAWAL} ⭐</b>"
            )
            logger.info(f"Admin {user_id} set minimum withdrawal to {MIN_WITHDRAWAL}")
        except ValueError:
            await update.message.reply_html(translate_text("❌ Please enter a valid integer number (e.g., 200)."))
        return

    # Handle gift chat ID input (Step 2)
    if context.user_data.get('gift_state') == 'waiting_for_chat_id':
        await process_gift_chat_id(update, context, text)
        return
    
    # Handle "1" as payment shortcut after /pingme (Step 3 shortcut)
    if context.user_data.get('gift_state') == 'waiting_for_payment' and text.strip() == "1":
        if not is_admin(user_id):
            return
        # Treat "1" as payment confirmation - process gift automatically
        logger.info(f"Admin {user_id}: Received '1' as payment shortcut, processing gift")
        await update.message.reply_html(translate_text("✅ <b>Payment confirmed!</b>\n\n🎂 <b>Processing gift...</b>", user_id=user_id))
        await process_gift_after_payment(update, context)
        return
    
    # Handle broadcast text (admin only, waiting flag set via /broadcast)
    if user_id in broadcast_waiting and update.effective_chat.type == "private":
        if not is_admin(user_id):
            broadcast_waiting.discard(user_id)
            return
        await perform_broadcast(update, context, update.message)
        broadcast_waiting.discard(user_id)
        return
    
    # Handle admin setting crypto addresses
    if user_id in admin_setting_crypto and update.effective_chat.type == "private":
        if not is_admin(user_id):
            admin_setting_crypto.pop(user_id, None)
            return
        
        coin_name = admin_setting_crypto[user_id]
        address = text.strip()
        
        if not address:
            await update.message.reply_html(t("send_valid_address", user_id=user_id))
            return
        
        # Determine network based on coin
        network_map = {
            "litecoin": "",
            "bitcoin": "",
            "ethereum": "ERC-20",
            "solana": "",
            "ton": "",
            "usdt_bep20": "BEP-20",
            "usdc_erc20": "ERC-20",
            "monero": ""
        }
        
        network = network_map.get(coin_name, "")
        
        # Save address
        crypto_addresses[coin_name] = {
            "address": address,
            "network": network
        }
        db.replace_crypto_addresses(dict(crypto_addresses))
        save_data()
        
        coin_display = {
            "litecoin": "Litecoin",
            "bitcoin": "Bitcoin",
            "ethereum": "Ethereum",
            "solana": "Solana",
            "ton": "TON",
            "usdt_bep20": "USDT BEP-20",
            "usdc_erc20": "USDC ERC-20",
            "monero": "Monero"
        }
        
        admin_setting_crypto.pop(user_id, None)
        
        network_text = f"\n<b>Network:</b> {network}" if network else ""
        
        await update.message.reply_html(
            f"✅ <b>Address saved successfully!</b>\n\n"
            f"💰 <b>Coin:</b> {coin_display[coin_name]}\n"
            f"📍 <b>Address:</b> <code>{address}</code>{network_text}"
        )
        logger.info(f"Admin {user_id} set {coin_name} address: {address}")
        return
    
    # Handle mines bet amount input
    if context.user_data.get('waiting_for_mines_bet'):
        if update.effective_chat.type != "private":
            return
        
        try:
            bet_amount = int(text)
            balance = get_user_balance(user_id)
            
            if bet_amount < 1:
                await update.message.reply_html(
                    "❌ <b>Invalid Bet Amount</b>\n\n"
                    "Minimum bet is <b>1 ⭐</b>"
                )
                return
            
            if bet_amount > balance:
                await update.message.reply_html(
                    f"❌ <b>Insufficient Balance</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 <b>Your Balance:</b> <b>{balance:,} ⭐</b>\n"
                    f"💵 <b>Requested:</b> <b>{bet_amount:,} ⭐</b>\n"
                    f"📊 <b>Shortage:</b> <b>{bet_amount - balance:,} ⭐</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━"
                )
                return
            
            grid_size = context.user_data.get('mines_grid_size')
            num_mines = context.user_data.get('mines_num_mines')
            
            if not grid_size or not num_mines:
                await update.message.reply_html(translate_text("❌ Error: Game settings not found. Please start again with /mines", user_id=user_id))
                context.user_data['waiting_for_mines_bet'] = False
                return
            
            # Deduct bet
            if not is_admin(user_id):
                adjust_user_balance(user_id, -bet_amount, game=True)
                user_balances[user_id] = get_user_balance(user_id)  # Sync memory cache

            # Create game
            game = MinesGame(user_id, grid_size, num_mines, bet_amount)
            mines_games[user_id] = game
            
            context.user_data['waiting_for_mines_bet'] = False
            context.user_data['mines_grid_size'] = None
            context.user_data['mines_num_mines'] = None
            
            # Show game
            message = format_mines_game_message(game)
            keyboard = create_mines_grid_keyboard(game)
            await update.message.reply_html(message, reply_markup=keyboard)
            
        except ValueError:
            await update.message.reply_html(translate_text("❌ Please enter a valid number.", user_id=user_id))
        return

    # Handle blackjack custom bet input
    if context.user_data.get("bj_custom_bet_pending"):
        pending = context.user_data.pop("bj_custom_bet_pending")
        try:
            bet = int(text)
            if bet < 10:
                await update.message.reply_html(t("bj_min_bet", user_id=user_id))
                return

            balance = get_user_balance(user_id)
            if balance < bet:
                await update.message.reply_html(
                    f"❌ Insufficient balance!\n💰 Your balance: {balance} ⭐"
                )
                return

            if user_id in blackjack_sessions:
                await update.message.reply_html(t("bj_active_game", user_id=user_id))
                return

            await bj_start_game(context, update, user_id, bet)

        except ValueError:
            await update.message.reply_html(
                "❌ Please enter a valid star amount (e.g. <code>150</code>)"
            )
        return

    if context.user_data.get('waiting_for_custom_amount'):
        try:
            amount = int(text)
            if amount < 1:
                await update.message.reply_html(translate_text("❌ Minimum deposit is 1 ⭐", user_id=user_id))
                return
            if amount > 10000:
                await update.message.reply_html(translate_text("❌ Maximum deposit is 10000 ⭐", user_id=user_id))
                return

            context.user_data['waiting_for_custom_amount'] = False
            
            title = f"Deposit {amount} Stars"
            description = f"Add {amount} ⭐ to your game balance"
            payload = f"deposit_{amount}_{user_id}"
            prices = [LabeledPrice("Stars", amount)]
            
            await update.message.reply_invoice(
                title=title,
                description=description,
                payload=payload,
                provider_token=PROVIDER_TOKEN,
                currency="XTR",
                prices=prices
            )
        except ValueError:
            await update.message.reply_html(translate_text("❌ Please enter a valid number.", user_id=user_id))
        return
    
    if context.user_data.get('withdraw_state') == 'waiting_amount':
        # Only respond in private chats (DM), not in groups
        if update.effective_chat.type != "private":
            return  # Silently ignore messages in groups
        
        withdraw_type = context.user_data.get('withdraw_type', 'stars')
        
        try:
            if withdraw_type == 'crypto':
                # Crypto withdrawal: accept USD amount and check crypto balance
                try:
                    amount_usd = float(text)
                    min_crypto_usd = 5.0
                    
                    if amount_usd < min_crypto_usd:
                        await update.message.reply_html(
                            f"❌ Minimum withdrawal is ${min_crypto_usd:.0f}"
                        )
                        return
                    
                    # Check crypto balance
                    crypto_balance = user_crypto_balances.get(user_id, 0.0)
                    
                    if amount_usd > crypto_balance:
                        await update.message.reply_html(
                            f"❌ <b>Insufficient crypto balance!</b>\n\n"
                            f"Your crypto balance: <b>${crypto_balance:.2f}</b>\n"
                            f"Requested: <b>${amount_usd:.2f}</b>"
                        )
                        return
                    
                    # Store USD amount for crypto withdrawal
                    context.user_data['withdraw_amount_usd'] = amount_usd
                    context.user_data['withdraw_amount'] = None  # Not using stars
                    context.user_data['withdraw_state'] = 'waiting_address'
                    
                    await update.message.reply_html(
                        f"💎 <b>Withdrawal Amount:</b> ${amount_usd:.2f}\n\n"
                        f"📍 <b>Enter your crypto wallet address:</b>"
                    )
                except ValueError:
                    await update.message.reply_html(translate_text("❌ Please enter a valid number (e.g., 10 or 10.50)"))
            else:
                # Stars withdrawal: accept stars amount
                amount = int(text)
                balance = get_user_balance(user_id)
                
                if amount < MIN_WITHDRAWAL:
                    await update.message.reply_html(t("min_withdrawal_msg", user_id=user_id, min=MIN_WITHDRAWAL))
                    return
                
                if amount > balance:
                    await update.message.reply_html(
                        f"❌ Insufficient balance!\n\n"
                        f"Your balance: {balance} ⭐\n"
                        f"Requested: {amount} ⭐"
                    )
                    return
                
                context.user_data['withdraw_amount'] = amount
                context.user_data['withdraw_amount_usd'] = None
                context.user_data['withdraw_state'] = 'waiting_address'
                
                ton_amount = round(amount * STARS_TO_TON, 8)
                
                await update.message.reply_html(
                    translate_text(
                        f"💎 <b>Withdrawal Amount:</b> {amount} ⭐\n"
                        f"💰 <b>TON Amount:</b> {ton_amount}\n\n"
                        f"📍 <b>Enter your TON wallet address:</b>"
                    )
                )
        except ValueError:
            await update.message.reply_html(translate_text("❌ Please enter a valid number.", user_id=user_id))
        return
    
    if context.user_data.get('withdraw_state') == 'waiting_address':
        # Only respond in private chats (DM), not in groups
        if update.effective_chat.type != "private":
            return  # Silently ignore messages in groups
        
        withdraw_type = context.user_data.get('withdraw_type', 'stars')
        
        if withdraw_type == 'crypto':
            # Crypto withdrawal: validate address
            is_valid, coin_name = is_valid_crypto_address(text)
            
            if not is_valid:
                await update.message.reply_html(
                    f"❌ <b>Invalid crypto address!</b>\n\n"
                    f"Please enter a valid cryptocurrency wallet address.\n\n"
                    f"Supported formats:\n"
                    f"• Bitcoin (1..., 3..., bc1...)\n"
                    f"• Litecoin (L..., M..., ltc1...)\n"
                    f"• Ethereum (0x...)\n"
                    f"• TON (UQ..., EQ...)\n"
                    f"• Solana (base58)\n"
                    f"• Monero (4...)\n"
                    f"• USDT/USDC (0x...)"
                )
                return
            
            context.user_data['withdraw_address'] = text
            context.user_data['detected_coin'] = coin_name
            amount_usd = context.user_data.get('withdraw_amount_usd', 0)
            crypto_balance = user_crypto_balances.get(user_id, 0.0)
            
            keyboard = [
                [
                    InlineKeyboardButton(translate_text("✅ Confirm", user_id=user_id), callback_data="confirm_withdraw"),
                    InlineKeyboardButton(translate_text("❌ Cancel", user_id=user_id), callback_data="cancel_withdraw"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            sent_summary = await update.message.reply_html(
                f"📋 <b>Withdrawal Summary</b>\n\n"
                f"💎 <b>Amount:</b> ${amount_usd:.2f}\n"
                f"💰 <b>Your Crypto Balance:</b> ${crypto_balance:.2f}\n"
                f"🎲 <b>Network:</b> {coin_name}\n"
                f"🏦 <b>Address:</b>\n<code>{text}</code>\n\n"
                f"Please confirm the withdrawal details above.",
                reply_markup=reply_markup
            )
            register_menu_owner(sent_summary, user_id)
        else:
            # Stars withdrawal: validate TON address
            if not is_valid_ton_address(text):
                await update.message.reply_html(
                    f"❌ <b>Invalid TON address!</b>\n\n{translate_text('Please enter a valid TON wallet address.', user_id=user_id)}"
                )
                return
            
            context.user_data['withdraw_address'] = text
            
            stars_amount = context.user_data.get('withdraw_amount', 0)
            ton_amount = round(stars_amount * STARS_TO_TON, 8)
            
            keyboard = [
                [
                    InlineKeyboardButton(translate_text("✅ Confirm", user_id=user_id), callback_data="confirm_withdraw"),
                    InlineKeyboardButton(translate_text("❌ Cancel", user_id=user_id), callback_data="cancel_withdraw"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            sent_summary = await update.message.reply_html(
                translate_text(
                    f"📋 <b>Withdrawal Summary:</b>\n\n"
                    f"⭐ Stars: {stars_amount}\n"
                    f"💎 TON: {ton_amount}\n"
                    f"🏦 Address: <code>{text}</code>\n\n"
                    f"Confirm withdrawal?"
                ),
                reply_markup=reply_markup
            )
            register_menu_owner(sent_summary, user_id)
        return


@handle_errors
async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    precheckout_user_id = query.from_user.id
    if is_frozen(precheckout_user_id) and not is_admin(precheckout_user_id):
        await query.answer(ok=False, error_message="Your account is frozen. Contact an admin.")
        return
    await query.answer(ok=True)


@handle_errors
async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payment = update.message.successful_payment

    amount = payment.total_amount
    payload = payment.invoice_payload

    # Check if this is a gift payment
    if payload and payload.startswith('gift_payment_'):
        # This is a gift payment - process gift automatically
        logger.info(f"Admin {user_id}: Gift payment received, processing gift automatically")
        await process_gift_after_payment(update, context)
        return

    # Block frozen users from depositing (payment already went through precheckout, but just in case)
    if is_frozen(user_id) and not is_admin(user_id):
        await update.message.reply_html(
            "🧊 <b>Your account is frozen.</b>\n\n"
            "Payment received but your account is frozen. Contact an admin to resolve."
        )
        # Still credit the balance since payment already processed by Telegram
        adjust_user_balance(user_id, amount)
        return

    # Regular deposit payment
    adjust_user_balance(user_id, amount)
    balance = get_user_balance(user_id)
    
    await update.message.reply_html(
        f"✅ <b>Payment successful!</b>\n\n"
        f"💰 Added: <b>{amount} ⭐</b>\n"
        f"💳 New balance: <b>{balance:,} ⭐</b>"
    )


@handle_errors
async def wd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set minimum withdrawal amount (admin only)"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    # Check if admin provided amount directly
    if context.args and len(context.args) >= 1:
        try:
            amount = int(context.args[0])
            if amount < 1:
                await update.message.reply_html(translate_text("❌ Minimum withdrawal must be at least 1 ⭐", user_id=user_id))
                return
            
            global MIN_WITHDRAWAL
            MIN_WITHDRAWAL = amount
            save_data()
            await update.message.reply_html(
                f"✅ <b>Minimum withdrawal updated!</b>\n\n"
                f"💰 New minimum: <b>{MIN_WITHDRAWAL} ⭐</b>"
            )
            logger.info(f"Admin {user_id} set minimum withdrawal to {MIN_WITHDRAWAL}")
            return
        except ValueError:
            await update.message.reply_html(translate_text("❌ Please enter a valid integer number.", user_id=user_id))
            return
    
    # Prompt admin for amount
    context.user_data['waiting_for_min_withdrawal'] = True
    await update.message.reply_html(
        "💰 <b>Set Minimum Withdrawal</b>\n\n"
        f"Current minimum: <b>{MIN_WITHDRAWAL} ⭐</b>\n\n"
        "Send the new minimum withdrawal amount in stars (integer only).\n"
        "Example: 200"
    )


# Gift system configuration
GIFT_STARS = 15  # Telegram's gift limit
PAYMENT_STARS = 1  # Payment amount for gift process


@handle_errors
async def gift_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start gift process - Step 1: Ask for chat ID or username"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized"))
        return
    
    # Reset any previous state
    context.user_data['gift_state'] = 'waiting_for_chat_id'
    context.user_data['gift_target_user_id'] = None
    context.user_data['gift_target_username'] = None
    
    await update.message.reply_html(
        "📄 <b>Please send the chat ID or username of the recipient</b>"
    )
    
    logger.info(f"Admin {user_id} started gift process - waiting for chat ID")


@handle_errors
async def pingme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden command - Step 3: Create payment invoice"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        return  # Silently ignore non-admins
    
    # Delete the command message to hide it
    try:
        await update.message.delete()
    except Exception:
        pass
    
    # Check if target user is set (Step 2 completed)
    if context.user_data.get('gift_state') != 'waiting_for_pingme':
        await update.message.reply_html(
            "❌ <b>Please complete the gift process first.</b>\n\n"
            "Use /gift to start, then provide chat ID or username."
        )
        return
    
    target_user_id = context.user_data.get('gift_target_user_id')
    if not target_user_id:
        await update.message.reply_html(translate_text("❌ Target user not set. Use /gift to start.", user_id=user_id))
        return
    
    # Create payment invoice for 1 Star
    try:
        prices = [LabeledPrice("Gift Payment", PAYMENT_STARS)]
        payload = f"gift_payment_{user_id}_{target_user_id}"
        
        await update.message.reply_invoice(
            title="🎂 Gift Payment",
            description="Payment for sending Telegram gift",
            payload=payload,
            provider_token=PROVIDER_TOKEN,
            currency="XTR",  # Telegram Stars currency
            prices=prices,
            start_parameter="gift"
        )
        
        # Inform admin about "1" shortcut
        await update.message.reply_html(
            "💡 <b>Tip:</b> You can also send <b>1</b> to confirm payment and process the gift automatically."
        )
        
        context.user_data['gift_state'] = 'waiting_for_payment'
        logger.info(f"Admin {user_id} created gift payment invoice for target {target_user_id}")
    except Exception as e:
        logger.error(f"Error creating gift payment invoice: {e}", exc_info=True)
        await update.message.reply_html(
            f"❌ <b>Failed to create payment invoice.</b>\n\n"
            f"Error: {str(e)}"
        )


@handle_errors
async def user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all users (admin only)"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized"))
        return
    
    try:
        # Get all users from profiles
        all_user_ids = list(user_profiles.keys())
        
        if not all_user_ids:
            await update.message.reply_html(translate_text("📋 <b>User List</b>\n\nNo users found."))
            return
        
        # Sort by user ID
        all_user_ids.sort()
        
        # Check if pagination is needed (Telegram message limit is 4096 characters)
        total_users = len(all_user_ids)
        
        # Build user list
        user_list_text = f"📋 <b>User List</b>\n\n"
        user_list_text += f"Total users: <b>{total_users}</b>\n\n"
        
        # List users (limit to avoid message too long)
        max_users_per_message = 50
        users_to_show = all_user_ids[:max_users_per_message]
        
        for idx, uid in enumerate(users_to_show, 1):
            profile = user_profiles.get(uid, {})
            username = profile.get('username', '')
            display_name = profile.get('display_name', '')
            balance = get_user_balance(uid)
            
            # Format username display
            if username:
                user_display = f"@{username}"
            elif display_name:
                user_display = display_name
            else:
                user_display = f"User {uid}"
            
            # Check if banned
            banned_status = "🔨" if uid in banned_users else ""
            
            user_list_text += f"{idx}. <code>{uid}</code> - {user_display} {banned_status}\n"
        
        if total_users > max_users_per_message:
            user_list_text += f"\n... and {total_users - max_users_per_message} more users"
        
        await update.message.reply_html(user_list_text)
        
        logger.info(f"Admin {user_id} viewed user list ({total_users} users)")
        
    except Exception as e:
        logger.error(f"Error in user_command: {e}", exc_info=True)
        await update.message.reply_html(
            "❌ <b>An error occurred while displaying user list.</b>\n\n"
            "Please try again later."
        )


@handle_errors
async def com_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all available commands for users"""
    if not update.message:
        return
    
    commands_text = t("available_commands")
    
    try:
        await update.message.reply_html(commands_text)
    except Exception as e:
        logger.error(f"Error in com_command: {e}", exc_info=True)
        # Fallback to plain text (remove HTML tags)
        plain_text = commands_text.replace("<b>", "").replace("</b>", "")
        await update.message.reply_text(plain_text)


@handle_errors
async def cmd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full command reference: user + admin lists (admins only)."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("admin_only", user_id=user_id))
        return

    user_cmds = (
        "📋 <b>All user commands</b>\n\n"
        "<b>Start &amp; help</b>\n"
        "• /start — Welcome &amp; menu\n"
        "• /help — Support / help (alias of /support in this bot)\n"
        "• /support — Tickets &amp; help\n"
        "• /com — Short public command list\n"
        "• /cancel — Cancel current flow\n\n"
        "<b>Balance &amp; money</b>\n"
        "• /balance or /bal — Balance\n"
        "• /deposit or /depo — Deposit Stars\n"
        "• /withdraw — Withdraw Stars (DM)\n"
        "• /custom — Custom deposit amount\n\n"
        "<b>Games &amp; play</b>\n"
        "• /play — Game menu\n"
        "• /mines — Mines game\n"
        "• /dice, /bowl, /dart, /arrow (dart), /football, /basket — Emoji games\n"
        "• /demo — Demo games (no bet)\n"
        "• /predict — Predictor game\n"
        "• /cflip, /cf — Coinflip\n"
        "• /blackjack or /bj — Blackjack\n\n"
        "<b>Profile &amp; social</b>\n"
        "• /profile, /levels, /history, /matches, /leaderboard\n"
        "• /bonus, /weekly — Bonuses\n"
        "• /referral or /ref — Referrals\n"
        "• /tip — Tip stars\n\n"
        "<b>Other</b>\n"
        "• /hb or /housebal — House bankroll (public view)\n"
        "• /lang — Your language\n"
    )

    admin_cmds = (
        "👑 <b>All admin commands</b>\n\n"
        "<b>Overview</b>\n"
        "• /admin — Compact admin cheat sheet\n"
        "• /cmd — This full list (admin only)\n\n"
        "<b>Admins &amp; users</b>\n"
        "• /addadmin, /removeadmin, /listadmins\n"
        "• /user — User list\n"
        "• /ban, /unban, /freeze, /unfreeze\n\n"
        "<b>Balances</b>\n"
        "• /addbal, /removebal, /setbal, /resetbal, /transferbal\n"
        "• /topbal, /totalbal\n\n"
        "<b>Bot &amp; media</b>\n"
        "• /today — Stats dashboard\n"
        "• /video, /video status, /video remove\n"
        "• /broadcast or /bc — Broadcast to users\n"
        "• /broadcastall — All bots (network)\n"
        "• /demo — Test games without bets\n"
        "• /steal — Rebrand (name, links, support)\n"
        "• /gift — Send gift\n"
        "• /cg — Gift comment\n"
        "• /setlang — Global default language\n"
        "• /set — Crypto deposit addresses\n"
        "• /emoji, /skip — Emoji mapping flow\n"
        "• /wd — Min withdrawal (Stars)\n"
        "• /hb or /housebal — Set casino bankroll (admin mode)\n\n"
        "<b>Events</b>\n"
        "• /rainevent, /jackpot, /doubledeposit, /tripledeposit\n"
        "• /goldenhour, /stopgoldenhour, /cashbackevent, /stopcashback, /eventstatus\n\n"
        "<b>Multi-bot network</b>\n"
        "• /addbot, /removebot, /syncbot, /syncall, /crossban\n"
        "• /sharedblacklist, /botnetwork, /centralstats\n\n"
        "<b>Hidden / misc</b>\n"
        "• /pingme — Admin gift flow helper\n"
    )

    try:
        await update.message.reply_html(user_cmds)
        await update.message.reply_html(admin_cmds)
    except Exception as e:
        logger.error(f"Error in cmd_command: {e}", exc_info=True)


@handle_errors
async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change user language preference"""
    if not update.message:
        return

    user_id = update.effective_user.id
    current_lang = user_languages.get(user_id, "en")

    lang_options = [
        ("🇬🇧 English", "en"),
        ("🇷🇺 Ð ÑÑÑÐºÐ¸Ð¹", "ru"),
        ("🇩🇪 Deutsch", "de"),
        ("🇫🇷 Français", "fr"),
        ("🇨🇳 中文", "zh"),
    ]

    # Build buttons — 2 per row, checkmark on current
    keyboard = []
    row = []
    for label, code in lang_options:
        mark = " ✓" if code == current_lang else ""
        row.append(InlineKeyboardButton(f"{label}{mark}", callback_data=f"set_lang_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    lang_names = {"en": "English", "ru": "Ð ÑÑÑÐºÐ¸Ð¹", "de": "Deutsch", "fr": "Français", "zh": "中文"}
    current_name = lang_names.get(current_lang, "English")

    await update.message.reply_html(
        f"🌐 <b>Language Selection</b>\n\n"
        f"Current language: <b>{current_name}</b>\n\n"
        f"Select your preferred language:",
        reply_markup=reply_markup
    )


@handle_errors
async def setlang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change bot language (admin only - for global default)"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(t("admin_only", user_id=user_id))
        return
    
    global bot_language
    
    # Toggle language
    if bot_language == "en":
        bot_language = "ru"
        message = t("language_changed_ru", user_id=user_id)
    else:
        bot_language = "en"
        message = t("language_changed_en", user_id=user_id)
    
    db.set_bot_language(bot_language)
    await update.message.reply_html(message)
    logger.info(f"Admin {user_id} changed bot language to {bot_language}")


@handle_errors
async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Support command - create or view tickets"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Check if command is in group chat
    if not is_private_chat(update):
        keyboard = [
            [InlineKeyboardButton(t("click_here"), url="https://t.me/Iibratebot?start=support")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_html(
            t("please_use_private"),
            reply_markup=reply_markup
        )
        return
    
    # Try to use template first
    template_sent = await send_template_message(
        update.message, context, "help", user_id
    )
    
    if template_sent:
        return
    
    # Fallback to default message
    keyboard = [
        [
            InlineKeyboardButton(t("create_ticket"), callback_data="support_create_ticket"),
            InlineKeyboardButton(t("my_ticket"), callback_data="support_my_tickets")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_html(
        t("support_answers"),
        reply_markup=reply_markup
    )


@handle_errors
async def cg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change gift comment (admin only)"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized"))
        return
    
    # Check if admin provided new comment directly
    if context.args and len(context.args) > 0:
        new_comment = ' '.join(context.args)
        global gift_comment
        gift_comment = new_comment
        db.set_gift_comment(new_comment)
        await update.message.reply_html(
            f"✅ <b>Gift comment updated!</b>\n\n"
            f"New comment: <b>{gift_comment}</b>"
        )
        logger.info(f"Admin {user_id} changed gift comment to: {gift_comment}")
        return
    
    # Show current comment and prompt for new one
    await update.message.reply_html(
        translate_text(
            f"💬 <b>Change Gift Comment</b>\n\n"
            f"Current comment: <b>{gift_comment}</b>\n\n"
            f"Usage: /cg [new comment]\n\n"
            f"Example: /cg 💰 @Iibrate - be with the best!"
        )
    )


async def process_gift_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Process chat ID or username input - Step 2"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        return
    
    target_user_id = None
    target_username = None
    
    # Try to parse as user_id (numeric)
    try:
        target_user_id = int(text.strip())
        target_username = str(target_user_id)
    except ValueError:
        # Try to find by username
        username = text.strip()
        if username.startswith('@'):
            username = username[1:]
        username_lower = username.lower()
        
        if username_lower in username_to_id:
            target_user_id = username_to_id[username_lower]
            target_username = username
        else:
            await update.message.reply_html(
                "❌ <b>User not found!</b>\n\n"
                "Please provide a valid username or chat ID.\n\n"
                "Examples:\n"
                "• 123456789 (chat ID)\n"
                "• @username (username)\n"
                "• username (username without @)"
            )
            return
    
    # Save target user
    context.user_data['gift_target_user_id'] = target_user_id
    context.user_data['gift_target_username'] = target_username
    context.user_data['gift_state'] = 'waiting_for_pingme'
    
    await update.message.reply_html(
        f"✅ <b>Target user set: {target_username or target_user_id}</b>\n\n"
        f"Now send /pingme to create payment invoice"
    )
    
    logger.info(f"Admin {user_id} set gift target: {target_user_id} ({target_username})")


async def process_gift_after_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically process gift after successful payment - Step 4"""
    user_id = update.effective_user.id
    target_user_id = context.user_data.get('gift_target_user_id')
    target_username = context.user_data.get('gift_target_username', str(target_user_id))
    
    if not target_user_id:
        logger.error(f"Gift processing failed: No target user ID for admin {user_id}")
        await update.message.reply_html(translate_text("❌ Target user not found. Gift process cancelled.", user_id=user_id))
        return
    
    try:
        # Get available gifts from Telegram API
        logger.info(f"Admin {user_id}: Getting available gifts from Telegram API")
        
        # Use get_available_gifts() method
        if hasattr(context.bot, 'get_available_gifts'):
            gifts_response = await context.bot.get_available_gifts()
        else:
            # Fallback: Use API directly
            gifts_response = await context.bot._post('getAvailableGifts', {})
        
        # Filter gifts where star_count <= 15
        available_gifts = []
        if hasattr(gifts_response, 'gifts'):
            gifts_list = gifts_response.gifts
        elif isinstance(gifts_response, dict) and 'gifts' in gifts_response:
            gifts_list = gifts_response['gifts']
        else:
            gifts_list = []
        
        for gift in gifts_list:
            star_count = getattr(gift, 'star_count', None) or gift.get('star_count', 0)
            if star_count <= GIFT_STARS:
                available_gifts.append(gift)
        
        if not available_gifts:
            logger.error(f"No suitable gifts found (all exceed {GIFT_STARS} stars)")
            await update.message.reply_html(
                f"❌ <b>No suitable gifts available.</b>\n\n"
                f"All available gifts exceed {GIFT_STARS} stars limit."
            )
            # Reset state
            context.user_data['gift_state'] = None
            context.user_data['gift_target_user_id'] = None
            context.user_data['gift_target_username'] = None
            return
        
        # Select gift closest to 15 stars (prefer highest <= 15)
        selected_gift = max(available_gifts, key=lambda g: getattr(g, 'star_count', 0) or g.get('star_count', 0))
        gift_id = getattr(selected_gift, 'id', None) or selected_gift.get('id')
        gift_stars = getattr(selected_gift, 'star_count', None) or selected_gift.get('star_count', 0)
        
        logger.info(f"Admin {user_id}: Selected gift ID {gift_id} with {gift_stars} stars")
        
        # Get template for gift command, or fallback to random message
        template_html, template_entities, template_reply_markup = get_template("gift")
        if template_html:
            # Replace variables in template
            target_user = update.effective_user if hasattr(update, 'effective_user') else None
            target_username = target_username if 'target_username' in locals() else f"User_{target_user_id}"
            gift_message = replace_template_variables(
                template_html,
                target_user_id,
                amount=gift_stars,
                balance=get_user_balance(target_user_id),
                username=target_username
            )
            logger.info(f"Using template for gift message to {target_user_id}")
        else:
            # Fallback to random gift message
            gift_message = get_random_gift_message()
            logger.info(f"Using random gift message for {target_user_id}")
        
        # Send gift to target user with gift message/note
        # Telegram Bot API uses 'message' parameter for gift notes
        gift_sent = False
        comment_sent_in_gift = False
        
        # Try with 'message' parameter first (official Telegram API parameter for gift notes)
        try:
            result = await context.bot._post(
                'sendGift',
                {
                    'user_id': target_user_id,
                    'gift_id': gift_id,
                    'message': gift_message
                }
            )
            gift_sent = True
            comment_sent_in_gift = True
            logger.info(f"✅ Sent gift with message/note (parameter: 'message') to {target_user_id}: {gift_message}")
        except Exception as e1:
            error_msg = str(e1).lower()
            logger.warning(f"Failed to send gift with 'message' parameter: {e1}")
            # Try with 'comment' parameter as fallback
            if 'message' in error_msg or 'unexpected' in error_msg or 'invalid' in error_msg:
                try:
                    result = await context.bot._post(
                        'sendGift',
                        {
                            'user_id': target_user_id,
                            'gift_id': gift_id,
                            'comment': gift_message
                        }
                    )
                    gift_sent = True
                    comment_sent_in_gift = True
                    logger.info(f"✅ Sent gift with message/note (parameter: 'comment') to {target_user_id}: {gift_message}")
                except Exception as e2:
                    logger.warning(f"Failed to send gift with 'comment' parameter: {e2}")
                    # Try with 'text' parameter as another fallback
                    try:
                        result = await context.bot._post(
                            'sendGift',
                            {
                                'user_id': target_user_id,
                                'gift_id': gift_id,
                                'text': gift_message
                            }
                        )
                        gift_sent = True
                        comment_sent_in_gift = True
                        logger.info(f"✅ Sent gift with message/note (parameter: 'text') to {target_user_id}: {gift_message}")
                    except Exception as e3:
                        # Last resort: send gift without message, then send message separately
                        logger.warning(f"None of the message parameters worked, sending gift without message: {e3}")
                        try:
                            result = await context.bot._post(
                                'sendGift',
                                {
                                    'user_id': target_user_id,
                                    'gift_id': gift_id
                                }
                            )
                            gift_sent = True
                            # Send gift message as separate message
                            try:
                                await context.bot.send_message(
                                    chat_id=target_user_id,
                                    text=gift_message
                                )
                                logger.info(f"Sent gift message as separate message to {target_user_id}: {gift_message}")
                            except Exception as msg_error:
                                logger.warning(f"Failed to send gift message separately: {msg_error}")
                        except Exception as e4:
                            logger.error(f"Error sending gift: {e4}", exc_info=True)
                            raise e4
        
        if not gift_sent:
            raise Exception("Failed to send gift after all attempts")
        
        logger.info(f"Admin {user_id}: Successfully sent gift {gift_id} ({gift_stars} stars) to {target_user_id}")
        
        # Send referral message to gift recipient IMMEDIATELY after gift is sent
        try:
            # Get or create referral code for recipient
            recipient_ref_code = get_or_create_referral_code(target_user_id)
            
            # Get bot username for referral link
            try:
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username if bot_info.username else "Iibratebot"
            except Exception:
                bot_username = "Iibratebot"  # Fallback
            
            referral_link = f"t.me/{bot_username}?start=ref-{recipient_ref_code}"
            
            referral_message = (
                f"Invite your friends using your special link and receive a <b>daily gift</b> worth 10% from their activity 💝🔗\n\n"
                f"Claim your gift link:👉 {referral_link}\n\n"
                f"✅ The more friends you invite, the bigger your <b>daily gifts</b>!°\n\n"
                f"Gifts are credited every day automatically"
            )
            
            await context.bot.send_message(
                chat_id=target_user_id,
                text=referral_message,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Sent referral message immediately to gift recipient {target_user_id}")
        except Exception as ref_error:
            logger.warning(f"Failed to send referral message to {target_user_id}: {ref_error}")
            # Continue even if referral message fails
        
        # Confirm success to admin (after referral message is sent)
        await update.message.reply_html(
            translate_text(
                f"✅ <b>Payment received!</b>\n\n"
                f"🎂 <b>Processing gift...</b>\n\n"
                f"✅ <b>Gift sent successfully to user {target_username or target_user_id}!</b>\n\n"
                f"Gift ID: <code>{gift_id}</code>\n"
                f"Stars: {gift_stars} ⭐"
            )
        )
        
        # Reset state
        context.user_data['gift_state'] = None
        context.user_data['gift_target_user_id'] = None
        context.user_data['gift_target_username'] = None
        
    except Exception as e:
        logger.error(f"Error processing gift after payment: {e}", exc_info=True)
        await update.message.reply_html(
            f"❌ <b>Failed to send gift.</b>\n\n"
            f"Error: {str(e)}\n\n"
            f"{translate_text('Please try again or contact support.', user_id=user_id)}"
        )
        # Reset state on error
        context.user_data['gift_state'] = None
        context.user_data['gift_target_user_id'] = None
        context.user_data['gift_target_username'] = None


@handle_errors
async def bankroll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show or set bankroll. Admins can set; everyone can view."""
    global casino_bankroll_usd
    user_id = update.effective_user.id

    # Admin setting value
    if is_admin(user_id):
        if context.args and len(context.args) >= 1:
            try:
                amount = float(context.args[0])
                casino_bankroll_usd = amount
                save_data()
                await update.message.reply_html(
                    f"✅ Bankroll updated.\n\n🏦 Casino Bankroll\n💵 USD: ${casino_bankroll_usd:,.2f}"
                )
                return
            except ValueError:
                pass  # fall through to prompt
        
        # Prompt admin for amount if not provided or invalid
        context.user_data['waiting_for_bankroll'] = True
        await update.message.reply_html(
            "🏦 <b>Set Casino Bankroll</b>\n\n"
            "Send the bankroll amount in USD (e.g., 2493.23)."
        )
        return
    
    # Non-admins: always read fresh live value from DB
    casino_bankroll_usd = db.get_casino_bankroll()
    await update.message.reply_html(
        f"🏦 <b>Casino Bankroll</b>\n\n"
        f"💵 <b>${casino_bankroll_usd:,.2f}</b>"
    )


@handle_errors
async def set_crypto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to set crypto addresses"""
    if not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ <b>You don't have permission to use this command.</b>", user_id=user_id))
        return
    
    if not context.args or len(context.args) == 0:
        await update.message.reply_html(
            "â¹ï¸  <b>Set Crypto Address</b>\n\n"
            "Usage: /set <coin_name>\n\n"
            "Available coins:\n"
            "• litecoin\n"
            "• bitcoin\n"
            "• ethereum\n"
            "• solana\n"
            "• ton\n"
            "• usdt_bep20\n"
            "• usdc_erc20\n"
            "• monero\n\n"
            "Example: /set ethereum\n\n"
            "After sending this command, send the address in the next message."
        )
        return
    
    coin_name = context.args[0].lower()
    valid_coins = ["litecoin", "bitcoin", "ethereum", "solana", "ton", "usdt_bep20", "usdc_erc20", "monero"]
    
    if coin_name not in valid_coins:
        await update.message.reply_html(
            "❌ <b>Invalid coin name!</b>\n\n"
            "Valid coins: litecoin, bitcoin, ethereum, solana, ton, usdt_bep20, usdc_erc20, monero"
        )
        return
    
    # Set waiting state for admin to send address
    admin_setting_crypto[user_id] = coin_name
    
    coin_display = {
        "litecoin": "Litecoin",
        "bitcoin": "Bitcoin",
        "ethereum": "Ethereum",
        "solana": "Solana",
        "ton": "TON",
        "usdt_bep20": "USDT BEP-20",
        "usdc_erc20": "USDC ERC-20",
        "monero": "Monero"
    }
    
    await update.message.reply_html(
        f"✅ <b>Setting address for {coin_display[coin_name]}</b>\n\n"
        f"Please send the {coin_display[coin_name]} address now."
    )


async def perform_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, source_message):
    """Copy the admin's message to all known users who started the bot."""
    admin_id = update.effective_user.id
    total = 0
    sent = 0
    errors = 0
    
    # We consider all known user_ids from profiles as started users
    target_users = list(user_profiles.keys())
    total = len(target_users)
    
    for uid in target_users:
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=source_message.chat_id,
                message_id=source_message.message_id
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Forbidden:
            errors += 1
        except Exception:
            errors += 1
    
    await context.bot.send_message(
        chat_id=admin_id,
        text=translate_text(
            f"✅ Broadcast finished.\n"
            f"Total users: {total}\n"
            f"Sent: {sent}\n"
            f"Failed: {errors}",
            user_id=admin_id
        )
    )


@handle_errors
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt admin for a message to broadcast to all users."""
    user_id = update.effective_user.id
    
    # Must be admin
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ Only admins can broadcast.", user_id=user_id))
        return
    
    # Only accept in private chat
    if update.effective_chat.type != "private":
        await update.message.reply_html(translate_text("❌ Use this command in DM with the bot.", user_id=user_id))
        return
    
    broadcast_waiting.add(user_id)
    await update.message.reply_html(
        translate_text(
            "📢 <b>Broadcast Mode</b>\n\n"
            "Send the message you want to broadcast.\n"
            "Supports text, photos, videos, audio (mp3), documents, etc.\n\n"
            "Use /cancel to exit."
        )
    )


# ══════════════════════════════════════════════════════════════════════════════
#  SPECIAL EVENT COMMANDS  (admin only)
# ══════════════════════════════════════════════════════════════════════════════

@handle_errors
async def rainevent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/rainevent [total_stars] [optional_max_recipients] — distribute stars randomly among active users."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("not_authorized", user_id=user_id))
        return
    if not context.args or len(context.args) < 1:
        await update.message.reply_html(
            "📖 <b>Usage:</b> /rainevent [total_stars] [max_recipients]\n"
            "Example: /rainevent 5000 20"
        )
        return
    try:
        total_stars = int(context.args[0])
        max_recipients = int(context.args[1]) if len(context.args) > 1 else 50
    except ValueError:
        await update.message.reply_html(t("invalid_whole_numbers", user_id=user_id))
        return
    if total_stars <= 0:
        await update.message.reply_html(t("amount_must_be_positive_admin", user_id=user_id))
        return

    # Collect active users (anyone in user_profiles who is not banned)
    candidates = [uid for uid in user_profiles.keys() if not db.is_user_banned(uid) and not is_admin(uid)]
    if not candidates:
        await update.message.reply_html(t("no_eligible_users", user_id=user_id))
        return

    import random as _random
    chosen = _random.sample(candidates, min(max_recipients, len(candidates)))
    share = total_stars // len(chosen)
    if share <= 0:
        await update.message.reply_html(t("amount_too_small", user_id=user_id))
        return

    sent = 0
    for uid in chosen:
        db.adjust_user_balance(uid, share)
        sent += 1
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"🌧️ <b>Rain Event!</b>\n\n"
                    f"You received <b>{share:,} ⭐</b> from the rain!\n"
                    f"Good luck! 🍀"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    await update.message.reply_html(
        f"🌧️ <b>Rain Event Complete!</b>\n\n"
        f"💰 Total distributed: <b>{share * sent:,} ⭐</b>\n"
        f"👥 Recipients: <b>{sent}</b>\n"
        f"⭐ Each received: <b>{share:,} Stars</b>"
    )
    logger.info(f"[RAIN] Admin {user_id} rained {share * sent:,} ⭐ on {sent} users")


@handle_errors
async def jackpot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/jackpot [stars] — set a jackpot that the next game winner will claim."""
    global active_jackpot_stars
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("not_authorized", user_id=user_id))
        return
    if not context.args or len(context.args) < 1:
        status = f"🏆 Active jackpot: <b>{int(active_jackpot_stars):,} ⭐</b>" if active_jackpot_stars > 0 else "No active jackpot."
        await update.message.reply_html(
            f"📖 <b>Usage:</b> /jackpot [stars]\n"
            f"Example: /jackpot 10000\n\n{status}"
        )
        return
    try:
        amount = int(context.args[0])
    except ValueError:
        await update.message.reply_html(t("invalid_amount_plain", user_id=user_id))
        return
    if amount <= 0:
        await update.message.reply_html(t("amount_must_be_positive_admin", user_id=user_id))
        return
    active_jackpot_stars = float(amount)
    await update.message.reply_html(
        f"🏆 <b>Jackpot Set!</b>\n\n"
        f"⭐ Amount: <b>{amount:,} Stars</b>\n"
        f"🎯 The next user to win any game will claim it!"
    )
    logger.info(f"[JACKPOT] Admin {user_id} set jackpot to {amount:,} ⭐")


@handle_errors
async def doubledeposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/doubledeposit — toggle 2x deposit bonus on/off."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("not_authorized", user_id=user_id))
        return
    global deposit_bonus_mult
    if deposit_bonus_mult == 2:
        deposit_bonus_mult = 1
        await update.message.reply_html(t("double_deposit_off", user_id=user_id))
        logger.info(f"[EVENT] Admin {user_id} disabled double deposit")
    else:
        deposit_bonus_mult = 2
        await update.message.reply_html(
            "🎁 <b>Double Deposit Bonus: ON!</b>\n\n"
            "All deposits will now receive <b>2x Stars</b>!"
        )
        logger.info(f"[EVENT] Admin {user_id} enabled double deposit")


@handle_errors
async def tripledeposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tripledeposit — toggle 3x deposit bonus on/off."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("not_authorized", user_id=user_id))
        return
    global deposit_bonus_mult
    if deposit_bonus_mult == 3:
        deposit_bonus_mult = 1
        await update.message.reply_html(t("triple_deposit_off", user_id=user_id))
        logger.info(f"[EVENT] Admin {user_id} disabled triple deposit")
    else:
        deposit_bonus_mult = 3
        await update.message.reply_html(
            "🎁 <b>Triple Deposit Bonus: ON!</b>\n\n"
            "All deposits will now receive <b>3x Stars</b>!"
        )
        logger.info(f"[EVENT] Admin {user_id} enabled triple deposit")


@handle_errors
async def goldenhour_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/goldenhour [hours] [optional_multiplier] — start a golden hour with boosted win multipliers."""
    global golden_hour_end_dt, golden_hour_mult_val
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("not_authorized", user_id=user_id))
        return
    if not context.args or len(context.args) < 1:
        status = (
            f"⏰ Active until: {golden_hour_end_dt.strftime('%H:%M:%S')}"
            if golden_hour_end_dt and datetime.now() < golden_hour_end_dt
            else "No active golden hour."
        )
        await update.message.reply_html(
            f"📖 <b>Usage:</b> /goldenhour [hours] [multiplier]\n"
            f"Example: /goldenhour 2 1.5\n\n{status}"
        )
        return
    try:
        hours = float(context.args[0])
        mult  = float(context.args[1]) if len(context.args) > 1 else 1.5
    except ValueError:
        await update.message.reply_html(t("invalid_args_numbers", user_id=user_id))
        return
    if hours <= 0 or mult <= 1:
        await update.message.reply_html(t("hours_must_positive", user_id=user_id))
        return
    golden_hour_end_dt  = datetime.now() + timedelta(hours=hours)
    golden_hour_mult_val = mult
    end_str = golden_hour_end_dt.strftime("%H:%M:%S")
    await update.message.reply_html(
        f"✨ <b>Golden Hour Started!</b>\n\n"
        f"🎯 Multiplier: <b>{mult}x</b> on all game wins\n"
        f"⏰ Duration: <b>{hours}h</b> (until {end_str})\n\n"
        f"All wins are boosted! 🚀"
    )
    logger.info(f"[EVENT] Admin {user_id} started golden hour: {mult}x for {hours}h")


@handle_errors
async def stopgoldenhour_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stopgoldenhour — end golden hour early."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("not_authorized", user_id=user_id))
        return
    global golden_hour_end_dt
    if not golden_hour_end_dt or datetime.now() >= golden_hour_end_dt:
        await update.message.reply_html(t("no_active_golden_hour", user_id=user_id))
        return
    golden_hour_end_dt = None
    await update.message.reply_html(t("golden_hour_stopped", user_id=user_id))
    logger.info(f"[EVENT] Admin {user_id} stopped golden hour early")


@handle_errors
async def cashbackevent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cashbackevent [percent] [hours] — refund X% of each loss for X hours."""
    global cashback_pct, cashback_end_dt, cashback_start_dt, _cashback_seen_ids
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("not_authorized", user_id=user_id))
        return
    if not context.args or len(context.args) < 2:
        status = (
            f"💸 Active: {cashback_pct}% until {cashback_end_dt.strftime('%H:%M:%S')}"
            if cashback_pct > 0 and cashback_end_dt and datetime.now() < cashback_end_dt
            else "No active cashback event."
        )
        await update.message.reply_html(
            f"📖 <b>Usage:</b> /cashbackevent [percent] [hours]\n"
            f"Example: /cashbackevent 10 3\n\n{status}"
        )
        return
    try:
        pct   = int(context.args[0])
        hours = float(context.args[1])
    except ValueError:
        await update.message.reply_html(t("invalid_args_numbers", user_id=user_id))
        return
    if not 1 <= pct <= 100:
        await update.message.reply_html(t("percent_1_100", user_id=user_id))
        return
    if hours <= 0:
        await update.message.reply_html(t("hours_positive", user_id=user_id))
        return
    cashback_pct       = pct
    cashback_end_dt    = datetime.now() + timedelta(hours=hours)
    cashback_start_dt  = datetime.now()
    _cashback_seen_ids = set()
    end_str = cashback_end_dt.strftime("%H:%M:%S")
    await update.message.reply_html(
        f"💸 <b>Cashback Event Started!</b>\n\n"
        f"♻️ Refund: <b>{pct}%</b> of each losing bet\n"
        f"⏰ Duration: <b>{hours}h</b> (until {end_str})\n\n"
        f"Players will receive cashback automatically! 🤑"
    )
    logger.info(f"[EVENT] Admin {user_id} started cashback: {pct}% for {hours}h")


@handle_errors
async def stopcashback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stopcashback — end cashback event early."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(t("not_authorized", user_id=user_id))
        return
    global cashback_pct, cashback_end_dt, cashback_start_dt
    if not cashback_pct:
        await update.message.reply_html(t("no_active_cashback", user_id=user_id))
        return
    cashback_pct      = 0
    cashback_end_dt   = None
    cashback_start_dt = None
    await update.message.reply_html(t("cashback_stopped", user_id=user_id))
    logger.info(f"[EVENT] Admin {update.effective_user.id} stopped cashback early")


@handle_errors
async def eventstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/eventstatus — show all active special events."""
    if not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_html(t("not_authorized", user_id=update.effective_user.id))
        return
    now = datetime.now()
    lines = ["🎪 <b>Active Special Events</b>\n"]

    # Jackpot
    if active_jackpot_stars > 0:
        lines.append(f"🏆 Jackpot: <b>{int(active_jackpot_stars):,} ⭐</b> (waiting for first win)")
    else:
        lines.append("🏆 Jackpot: <i>inactive</i>")

    # Deposit bonus
    if deposit_bonus_mult > 1:
        lines.append(f"🎁 Deposit Bonus: <b>{deposit_bonus_mult}x</b> (active)")
    else:
        lines.append("🎁 Deposit Bonus: <i>inactive</i>")

    # Golden hour
    if golden_hour_end_dt and now < golden_hour_end_dt:
        remaining = golden_hour_end_dt - now
        mins = int(remaining.total_seconds() / 60)
        lines.append(f"✨ Golden Hour: <b>{golden_hour_mult_val}x</b> — {mins} min remaining")
    else:
        lines.append("✨ Golden Hour: <i>inactive</i>")

    # Cashback
    if cashback_pct > 0 and cashback_end_dt and now < cashback_end_dt:
        remaining = cashback_end_dt - now
        mins = int(remaining.total_seconds() / 60)
        lines.append(f"💸 Cashback: <b>{cashback_pct}%</b> — {mins} min remaining")
    else:
        lines.append("💸 Cashback: <i>inactive</i>")

    await update.message.reply_html("\n".join(lines))

async def stream_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable streaming message effect - admin only"""
    global streaming_enabled
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("âŒ Permission denied", user_id=user_id))
        return
    streaming_enabled = True
    await update.message.reply_html("✅ <b>Streaming ENABLED</b>\n3-5 word chunks, 150ms delays\nUse /streamoff to disable")


async def streamoff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable streaming message effect - admin only"""
    global streaming_enabled
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("âŒ Permission denied", user_id=user_id))
        return
    streaming_enabled = False
    await update.message.reply_html("✅ <b>Streaming DISABLED</b> - Normal messages\nUse /stream to enable")




# ══════════════════════════════════════════════════════════════════════════════


async def bankroll_hourly_fluctuation(context: ContextTypes.DEFAULT_TYPE):
    """Every hour: randomly add or subtract $100–$10,000 from casino bankroll."""
    delta = round(random.uniform(100.0, 10000.0), 2)
    if random.choice([True, False]):
        adjust_bankroll_usd(delta)
    else:
        adjust_bankroll_usd(-delta)
    logger.info(f"[BANKROLL] Hourly fluctuation — bankroll now ${casino_bankroll_usd:,.2f}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled exception: {context.error}", exc_info=context.error)
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_html(
                translate_text(
                    "❌ <b>An unexpected error occurred</b>\n\n"
                    "Please try again later. If the problem persists, contact support."
                )
            )
    except Exception as e:
        logger.error(f"Error in error handler: {e}")


async def poll_pending_deposits(context: ContextTypes.DEFAULT_TYPE):
    """
    Job that runs every 30 seconds.
    Checks every pending OxaPay deposit and credits the user's balance
    when OxaPay reports the invoice as Paid.
    One bad record cannot kill the job — every deposit is wrapped in try/except.
    """
    try:
        pending = db.get_pending_deposits()
        if not pending:
            return
        logger.info(f"[POLL] Checking {len(pending)} pending deposit(s)…")

        for deposit in pending:
            track_id   = deposit["track_id"]
            user_id    = deposit["user_id"]
            amount_usd = deposit.get("amount_usd") or 0.0
            currency   = deposit.get("currency", "USDT")

            try:
                result = await oxapay.check_invoice(track_id)
                if result is None:
                    continue

                status = result.get("status", "").lower()

                if status == "paid":
                    # Double-credit guard
                    if db.deposit_already_credited(track_id):
                        logger.info(f"[POLL] Deposit {track_id} already credited — skipping")
                        continue

                    raw_pay = result.get("payAmount", "") or "0"
                    pay_amount = float(raw_pay) if str(raw_pay).strip() else 0.0

                    # Mark as paid in DB first (prevent race conditions)
                    db.mark_deposit_paid(track_id, pay_amount)

                    # Determine base stars to credit
                    if amount_usd > 0:
                        base_stars = int(amount_usd / STARS_TO_USD)
                    else:
                        base_stars = int(pay_amount / STARS_TO_USD)

                    # Apply deposit bonus multiplier (double/triple deposit event)
                    effective_mult = deposit_bonus_mult if deposit_bonus_mult > 1 else 1
                    stars_to_credit = base_stars * effective_mult

                    # Credit balance directly (bypass admin-skip guard)
                    db.adjust_user_balance(user_id, stars_to_credit)

                    bonus_note = ""
                    if effective_mult > 1:
                        bonus_note = f"\n🎁 <b>{effective_mult}x Deposit Bonus applied!</b>"

                    logger.info(
                        f"[POLL] Credited {stars_to_credit:,} ⭐ to user {user_id} "
                        f"for deposit {track_id} "
                        f"(payAmount={pay_amount} {currency}, usd=${amount_usd}, mult={effective_mult}x)"
                    )

                    # Notify the user
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"✅ <b>Deposit Confirmed!</b>\n\n"
                                f"💰 Received: <b>{pay_amount} {currency}</b>\n"
                                f"⭐ Credited: <b>{stars_to_credit:,} Stars</b>"
                                f"{bonus_note}\n\n"
                                f"Enjoy your games! 🎰"
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as notify_err:
                        logger.warning(
                            f"[POLL] Could not notify user {user_id}: {notify_err}"
                        )

                elif status in ("expired", "error"):
                    # Stop polling this invoice
                    db.mark_deposit_expired(track_id, status)
                    logger.info(f"[POLL] Deposit {track_id} marked as {status.lower()}")

            except Exception as e:
                logger.error(
                    f"[POLL] Error processing deposit {track_id}: {e}", exc_info=True
                )

    except Exception as e:
        logger.error(f"[POLL] Unhandled error in poll_pending_deposits: {e}", exc_info=True)

    # ── Jackpot notifications ─────────────────────────────────────────────────
    while _jackpot_notify_queue:
        try:
            jp_user_id, jp_amount = _jackpot_notify_queue.pop(0)
            await context.bot.send_message(
                chat_id=jp_user_id,
                text=(
                    f"🎰 <b>JACKPOT! You won!</b>\n\n"
                    f"🏆 <b>{jp_amount:,} Stars</b> have been added to your balance!\n\n"
                    f"Congratulations! 🎊"
                ),
                parse_mode=ParseMode.HTML,
            )
            logger.info(f"[JACKPOT] Notified user {jp_user_id} — won {jp_amount:,} ⭐")
        except Exception as e:
            logger.warning(f"[JACKPOT] Notification failed: {e}")

    # ── Cashback event processing ─────────────────────────────────────────────
    try:
        global cashback_pct, cashback_end_dt, cashback_start_dt
        if cashback_pct > 0 and cashback_end_dt:
            now = datetime.now()
            if now > cashback_end_dt:
                cashback_pct = 0
                cashback_end_dt = None
                cashback_start_dt = None
                logger.info("[EVENT] Cashback event expired.")
            elif cashback_start_dt:
                conn = db.get_db_connection()
                rows = conn.execute(
                    "SELECT id, user_id, bet_amount FROM game_history "
                    "WHERE won=0 AND timestamp > ?",
                    (cashback_start_dt.isoformat(),),
                ).fetchall()
                for row in rows:
                    gid = row["id"]
                    if gid in _cashback_seen_ids:
                        continue
                    cb_amount = int(row["bet_amount"] * cashback_pct / 100)
                    if cb_amount > 0:
                        db.adjust_user_balance(row["user_id"], cb_amount)
                        _cashback_seen_ids.add(gid)
                        try:
                            await context.bot.send_message(
                                chat_id=row["user_id"],
                                text=(
                                    f"💸 <b>Cashback!</b>\n\n"
                                    f"You received <b>{cb_amount:,} ⭐</b> back "
                                    f"({cashback_pct}% cashback event is active)."
                                ),
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception:
                            pass
    except Exception as e:
        logger.error(f"[CASHBACK] Processing error: {e}", exc_info=True)

    # ── Golden hour expiry ────────────────────────────────────────────────────
    try:
        global golden_hour_end_dt
        if golden_hour_end_dt and datetime.now() > golden_hour_end_dt:
            golden_hour_end_dt = None
            logger.info("[EVENT] Golden hour expired.")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-BOT NETWORK COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@handle_errors
async def addbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register a new bot in the network."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_html(
            "📡 <b>Add Bot to Network</b>\n\n"
            "Usage: /addbot [bot_token]\n"
            "Optional: /addbot [bot_token] [db_path]"
        )
        return

    token = context.args[0].strip()
    explicit_db_path = context.args[1].strip() if len(context.args) > 1 else None

    # Check if already registered
    existing = network_db.get_bot_by_token(token)
    if existing:
        await update.message.reply_html(
            f"⚠️ Bot <b>@{existing['username']}</b> is already registered in the network."
        )
        return

    # Validate token via Telegram API
    msg = await update.message.reply_html(t("validating_token", user_id=user_id))
    result = await validate_bot_token(token)
    if not result:
        await msg.edit_text("❌ Invalid or expired bot token.")
        return

    name, username = result

    # Determine db_path
    if explicit_db_path:
        db_path = explicit_db_path
    else:
        candidates = detect_db_path_for_token(token)
        # Filter out our own DB
        own_path = os.path.abspath(db.path)
        candidates = [c for c in candidates if os.path.abspath(c) != own_path]

        if len(candidates) == 1:
            db_path = candidates[0]
        elif len(candidates) > 1:
            listing = "\n".join(f"  {i+1}. <code>{c}</code>" for i, c in enumerate(candidates))
            await msg.edit_text(
                f"⚠️ Multiple databases found. Re-run with explicit path:\n"
                f"<code>/addbot {token} [path]</code>\n\nCandidates:\n{listing}",
                parse_mode=ParseMode.HTML
            )
            return
        else:
            await msg.edit_text(
                f"⚠️ Could not auto-detect database path.\n"
                f"Re-run: <code>/addbot {token} /full/path/to/bot_data.db</code>",
                parse_mode=ParseMode.HTML
            )
            return

    if not os.path.exists(db_path):
        await msg.edit_text(f"❌ Database file not found:\n<code>{db_path}</code>", parse_mode=ParseMode.HTML)
        return

    network_db.add_bot(token, name, username, db_path, user_id)
    total_bots = len(network_db.get_all_bots())

    await msg.edit_text(
        f"✅ <b>Bot registered successfully!</b>\n\n"
        f"👤 Name: <b>{name}</b>\n"
        f"📛 Username: @{username}\n"
        f"💾 DB: <code>{db_path}</code>\n"
        f"🌐 Network size: <b>{total_bots}</b> bot(s)",
        parse_mode=ParseMode.HTML
    )


@handle_errors
async def removebot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a bot from the network."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    if not context.args:
        bots = network_db.get_all_bots()
        if not bots:
            await update.message.reply_html(t("no_bots_network", user_id=user_id))
            return
        listing = "\n".join(f"  • <b>{b['name']}</b> (@{b['username']})" for b in bots)
        await update.message.reply_html(
            f"📡 <b>Remove Bot</b>\n\n"
            f"Usage: /removebot [bot_name]\n\n"
            f"Registered bots:\n{listing}"
        )
        return

    name = " ".join(context.args).strip()
    if network_db.remove_bot(name):
        await update.message.reply_html(f"✅ Bot '<b>{name}</b>' removed from network.")
    else:
        await update.message.reply_html(f"❌ Bot '<b>{name}</b>' not found in network.")


@handle_errors
async def syncbot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sync settings from this bot to a target bot (with confirmation)."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    if not context.args:
        await update.message.reply_html(
            "🔄 <b>Sync Bot Settings</b>\n\n"
            "Usage: /syncbot [token_or_name]\n\n"
            "Syncs: admins, crypto addresses, game settings, "
            "min withdrawal, bot identity, language, gift comment."
        )
        return

    target = context.args[0].strip()
    bot_info = network_db.get_bot_by_name(target)
    if not bot_info:
        bot_info = network_db.get_bot_by_token(target)
    if not bot_info:
        await update.message.reply_html(t("err_bot_not_in_network", user_id=user_id))
        return

    context.user_data["sync_target_bot"] = bot_info

    keyboard = [
        [
            InlineKeyboardButton(t("btn_confirm_sync", user_id=user_id), callback_data="network_sync_confirm"),
            InlineKeyboardButton(t("btn_cancel", user_id=user_id), callback_data="network_sync_cancel")
        ]
    ]
    await update.message.reply_html(
        f"🔄 <b>Sync Preview</b>\n\n"
        f"Target: <b>{bot_info['name']}</b> (@{bot_info['username']})\n"
        f"DB: <code>{bot_info['db_path']}</code>\n\n"
        f"<b>Will sync:</b>\n"
        f"  • Admin list\n"
        f"  • Crypto addresses\n"
        f"  • Min withdrawal\n"
        f"  • Bot identity\n"
        f"  • Bot language\n"
        f"  • Gift comment\n"
        f"  • Casino bankroll\n"
        f"  • Frozen users\n\n"
        f"⚠️ This will <b>overwrite</b> settings on the target bot.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


@handle_errors
async def syncall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sync settings from this bot to ALL bots in the network."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    bots = network_db.get_all_bots()
    if not bots:
        await update.message.reply_html(t("no_bots_registered", user_id=user_id))
        return

    msg = await update.message.reply_html(t("syncing_settings", user_id=user_id))
    source_path = os.path.abspath(db.path)
    results = []
    for bot_info in bots:
        if os.path.abspath(bot_info["db_path"]) == source_path:
            results.append(f"  ⏭ {bot_info['name']}: Skipped (self)")
            continue
        try:
            synced = sync_settings_to_bot(source_path, bot_info["db_path"])
            results.append(f"  ✅ {bot_info['name']}: OK ({len(synced)} items)")
        except Exception as e:
            results.append(f"  ❌ {bot_info['name']}: FAILED ({e})")

    report = "\n".join(results)
    await msg.edit_text(
        f"🔄 <b>Sync All Results</b>\n\n{report}",
        parse_mode=ParseMode.HTML
    )


@handle_errors
async def crossban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user across all bots in the network."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    target_user_id = None
    target_username = None
    reason = ""

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = update.message.reply_to_message.from_user.username
        reason = " ".join(context.args) if context.args else ""
    elif context.args:
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            arg = context.args[0].lstrip("@")
            found_id = username_to_id.get(arg.lower())
            if found_id:
                target_user_id = found_id
                target_username = arg
            else:
                await update.message.reply_html(t("err_user_not_found_plain", user_id=user_id))
                return
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    else:
        await update.message.reply_html(
            "🚫 <b>Cross-Ban User</b>\n\n"
            "Usage: /crossban [user_id] [reason]\n"
            "Or reply to a user's message with /crossban [reason]"
        )
        return

    if is_admin(target_user_id):
        await update.message.reply_html(t("cannot_crossban_admin", user_id=user_id))
        return

    msg = await update.message.reply_html(t("crossbanning_user", user_id=user_id))

    # 1. Add to shared blacklist
    this_bot_name = bot_identity.get("name", "Unknown")
    network_db.add_to_blacklist(target_user_id, target_username, reason, user_id, this_bot_name)

    # 2. Ban on this bot
    banned_users.add(target_user_id)
    conn = db.get_db_connection()
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (target_user_id,))
    conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (target_user_id,))
    conn.commit()

    # 3. Ban on all network bots
    bots = network_db.get_all_bots()
    source_path = os.path.abspath(db.path)
    ban_results = []
    for bot_info in bots:
        if os.path.abspath(bot_info["db_path"]) == source_path:
            ban_results.append(f"  ✅ {bot_info['name']}: OK (this bot)")
            continue
        success = crossban_user_on_bot(bot_info["db_path"], target_user_id, target_username)
        ban_results.append(
            f"  {'✅' if success else '❌'} {bot_info['name']}: {'OK' if success else 'FAILED'}"
        )

    # 4. Notify admins on all bots
    notify_text = (
        f"🚫 <b>CROSSBAN</b>\n\n"
        f"User: <code>{target_user_id}</code>"
        + (f" (@{target_username})" if target_username else "")
        + (f"\nReason: {reason}" if reason else "")
        + f"\nBanned by: <code>{user_id}</code>"
        + f"\nSource: {this_bot_name}"
    )
    admin_ids = db.get_all_admins() | admin_list
    for bot_info in bots:
        try:
            notify_bot = Bot(token=bot_info["token"])
            for aid in admin_ids:
                if aid == user_id:
                    continue  # Skip the admin who ran the command
                try:
                    await notify_bot.send_message(
                        chat_id=aid, text=notify_text, parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        except Exception:
            pass

    report = "\n".join(ban_results)
    await msg.edit_text(
        f"🚫 <b>Crossban Complete</b>\n\n"
        f"User: <code>{target_user_id}</code>"
        + (f" (@{target_username})" if target_username else "")
        + (f"\nReason: {reason}" if reason else "")
        + f"\n\n<b>Results:</b>\n{report}",
        parse_mode=ParseMode.HTML
    )


@handle_errors
async def sharedblacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the combined shared blacklist across all bots."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    source_bot_filter = None
    reason_filter = None
    export_csv = False
    if context.args:
        for arg in context.args:
            if arg.lower() == "csv":
                export_csv = True
            elif arg.startswith("bot:"):
                source_bot_filter = arg[4:]
            elif arg.startswith("reason:"):
                reason_filter = arg[7:]

    if export_csv:
        csv_bytes = network_db.export_blacklist_csv(
            source_bot=source_bot_filter, reason=reason_filter
        )
        bio = io.BytesIO(csv_bytes)
        bio.name = "shared_blacklist.csv"
        await update.message.reply_document(document=bio, caption="📋 Shared Blacklist Export")
        return

    entries = network_db.get_blacklist(source_bot=source_bot_filter, reason=reason_filter)
    if not entries:
        await update.message.reply_html(t("shared_blacklist_empty", user_id=user_id))
        return

    lines = []
    for e in entries[:50]:
        line = f"  • <code>{e['user_id']}</code>"
        if e.get('username'):
            line += f" @{e['username']}"
        if e.get('reason'):
            line += f" — {e['reason']}"
        line += f" [{e.get('source_bot', '?')}]"
        lines.append(line)

    text = f"📋 <b>Shared Blacklist</b> ({len(entries)} total)\n\n"
    if len(entries) > 50:
        text += "<i>(Showing first 50. Use <code>csv</code> to export all.)</i>\n\n"
    text += "\n".join(lines)
    text += "\n\n<i>Filters: /sharedblacklist [bot:name] [reason:text] [csv]</i>"

    await update.message.reply_html(text)


@handle_errors
async def botnetwork_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dashboard showing all bots, online/offline, stats, ping."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    bots = network_db.get_all_bots()
    if not bots:
        await update.message.reply_html(t("no_bots_registered", user_id=user_id))
        return

    msg = await update.message.reply_html(t("gathering_status", user_id=user_id))

    lines = []
    total_users = 0
    total_games = 0
    total_revenue = 0.0

    for bot_info in bots:
        online, latency = await ping_bot(bot_info["token"])
        status_icon = "🟢 ONLINE" if online else "🔴 OFFLINE"
        ping_str = f"{latency}ms" if online else "N/A"

        stats = get_bot_stats(bot_info["db_path"], time_filter="today")
        if stats:
            users = stats["user_count"]
            games = stats["games_count"]
            revenue = stats["profit"]
            total_users += users
            total_games += games
            total_revenue += revenue
        else:
            users = games = 0
            revenue = 0.0

        lines.append(
            f"<b>{bot_info['name']}</b> (@{bot_info['username']})\n"
            f"  {status_icon} | Ping: {ping_str}\n"
            f"  👥 Users: {users:,} | 🎮 Games today: {games:,}\n"
            f"  💰 Revenue today: {revenue:,.0f} ⭐ (${revenue * STARS_TO_USD:,.2f})"
        )

    text = (
        f"🌐 <b>Bot Network Dashboard</b>\n"
        f"{'━' * 28}\n\n"
        + "\n\n".join(lines)
        + f"\n\n{'━' * 28}\n"
        f"<b>TOTALS:</b> {total_users:,} users | "
        f"{total_games:,} games | "
        f"{total_revenue:,.0f} ⭐ (${total_revenue * STARS_TO_USD:,.2f}) revenue"
    )
    await msg.edit_text(text, parse_mode=ParseMode.HTML)


@handle_errors
async def centralstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Combined stats across all bots with time filter and export."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    time_filter = None
    export = False
    if context.args:
        for arg in context.args:
            if arg.lower() in ("today", "week", "month"):
                time_filter = arg.lower()
            elif arg.lower() == "export":
                export = True

    bots = network_db.get_all_bots()
    if not bots:
        await update.message.reply_html(t("no_bots_registered", user_id=user_id))
        return

    msg = await update.message.reply_html(t("gathering_stats", user_id=user_id))

    all_stats = []
    totals = {
        "user_count": 0, "games_count": 0, "wagered": 0, "payouts": 0,
        "profit": 0, "deposit_count": 0, "deposit_total_usd": 0,
        "withdrawal_count": 0, "withdrawal_total_stars": 0, "total_balance": 0
    }

    for bot_info in bots:
        stats = get_bot_stats(bot_info["db_path"], time_filter=time_filter)
        if stats:
            stats["bot_name"] = bot_info["name"]
            all_stats.append(stats)
            for key in totals:
                totals[key] += stats.get(key, 0)

    filter_label = time_filter.upper() if time_filter else "ALL TIME"

    if export:
        lines = [f"Central Stats Report — {filter_label}\n"]
        lines.append(f"Generated: {datetime.now().isoformat()}\n\n")
        for s in all_stats:
            lines.append(f"--- {s['bot_name']} ---\n")
            for k, v in s.items():
                if k != "bot_name":
                    lines.append(f"  {k}: {v}\n")
            lines.append("\n")
        lines.append(f"--- TOTALS ---\n")
        for k, v in totals.items():
            lines.append(f"  {k}: {v}\n")
        bio = io.BytesIO("".join(lines).encode("utf-8"))
        bio.name = f"central_stats_{filter_label.lower()}.txt"
        await update.message.reply_document(document=bio, caption=f"📊 Central Stats: {filter_label}")
        return

    def s(stars):
        return f"{stars:,.0f} ⭐ (${stars * STARS_TO_USD:,.2f})"

    per_bot_lines = []
    for st in all_stats:
        per_bot_lines.append(
            f"  • <b>{st['bot_name']}</b>: {st['user_count']:,} users | "
            f"{st['games_count']:,} games | P/L: {s(st['profit'])}"
        )

    text = (
        f"📊 <b>Central Stats — {filter_label}</b>\n\n"
        f"👥 Users: <b>{totals['user_count']:,}</b>\n"
        f"🎮 Games: <b>{totals['games_count']:,}</b>\n"
        f"💵 Wagered: {s(totals['wagered'])}\n"
        f"💸 Paid out: {s(totals['payouts'])}\n"
        f"🏠 House P/L: {s(totals['profit'])}\n"
        f"📥 Deposits: {totals['deposit_count']:,} (${totals['deposit_total_usd']:,.2f})\n"
        f"📤 Withdrawals: {totals['withdrawal_count']:,} ({s(totals['withdrawal_total_stars'])})\n"
        f"💰 Balances held: {s(totals['total_balance'])}\n\n"
        f"<b>Per Bot:</b>\n" + "\n".join(per_bot_lines)
        + "\n\n<i>Usage: /centralstats [today|week|month] [export]</i>"
    )
    await msg.edit_text(text, parse_mode=ParseMode.HTML)


@handle_errors
async def broadcastall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast a message to ALL users across ALL bots."""
    if not update.message:
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_html(translate_text("❌ You are not authorized", user_id=user_id))
        return

    if update.effective_chat.type != "private":
        await update.message.reply_html(t("use_dm_bot", user_id=user_id))
        return

    bots = network_db.get_all_bots()
    if not bots:
        await update.message.reply_html(t("no_bots_registered", user_id=user_id))
        return

    context.user_data["broadcastall_waiting"] = True
    bot_listing = "\n".join(f"  • <b>{b['name']}</b> (@{b['username']})" for b in bots)
    await update.message.reply_html(
        f"📢 <b>Broadcast All Mode</b>\n\n"
        f"Target bots:\n{bot_listing}\n\n"
        f"Send the message you want to broadcast.\n"
        f"Use /cancel to exit."
    )


# ── Multi-bot sync reload ────────────────────────────────────────────────────
_last_known_sync_ts = None

async def check_sync_reload(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job: detect if settings were synced by another bot and reload."""
    global _last_known_sync_ts
    try:
        current_ts = db._get_setting("_last_sync_ts")
        if current_ts and current_ts != _last_known_sync_ts:
            _last_known_sync_ts = current_ts
            logger.info(f"[SYNC] Detected new sync timestamp: {current_ts}. Reloading settings...")
            load_data()
            logger.info("[SYNC] Settings reloaded successfully.")
    except Exception as e:
        logger.error(f"[SYNC] Reload check failed: {e}")


def main():
    # Load saved data on startup
    load_data()
    
    # Monkey-patch Message.reply_html to support streaming
    from telegram import Message
    _original_reply_html = Message.reply_html
    
    async def streaming_reply_html(self, text: str, *args, **kwargs):
        """Wrapped reply_html that supports streaming mode"""
        global streaming_enabled
        
        if not streaming_enabled or len(text.split()) <= 5:
            # Normal mode or text too short
            return await _original_reply_html(self, text, *args, **kwargs)
        
        # Streaming mode: send in chunks
        words = text.split()
        chunk_size_min, chunk_size_max = 3, 5
        delay_sec = 0.15
        
        messages = []
        i = 0
        while i < len(words):
            chunk_size = random.randint(chunk_size_min, min(chunk_size_max, len(words) - i))
            messages.append(" ".join(words[i:i + chunk_size]))
            i += chunk_size
        
        last_msg = None
        for idx, chunk in enumerate(messages):
            try:
                last_msg = await _original_reply_html(self, chunk, *args, **kwargs)
                if idx < len(messages) - 1:
                    await asyncio.sleep(delay_sec)
            except Exception as e:
                logger.error(f"Streaming chunk error: {e}")
                remaining = " ".join(messages[idx:])
                return await _original_reply_html(self, remaining, *args, **kwargs)
        return last_msg
    
    # Apply the patch
    Message.reply_html = streaming_reply_html
    
    load_coinflip_stickers()
    
    # Build application with optimizations for 1,000,000+ concurrent users
    application = (
        Application.builder()
        .bot(EmojiAwareBot(BOT_TOKEN))
        .concurrent_updates(True)  # Process updates in parallel
        .build()
    )
    
    application.add_error_handler(error_handler)

    # OxaPay deposit polling — checks every 30 s, starts after 10 s
    application.job_queue.run_repeating(
        poll_pending_deposits, interval=30, first=10
    )

    # Multi-bot sync reload — detects external settings sync every 60 s
    application.job_queue.run_repeating(
        check_sync_reload, interval=60, first=30
    )

    # Bankroll hourly fluctuation — randomly adds/subtracts $100-$10,000 every hour
    application.job_queue.run_repeating(
        bankroll_hourly_fluctuation, interval=3600, first=300
    )

    # Basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", support_command))  # Alias for /support
    application.add_handler(CommandHandler("com", com_command))
    application.add_handler(CommandHandler("cmd", cmd_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("bal", balance_command))  # Alias
    application.add_handler(CommandHandler("deposit", deposit_command))
    application.add_handler(CommandHandler("depo", deposit_command))  # Alias
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(CommandHandler("custom", custom_deposit))
    application.add_handler(CommandHandler("play", play_command))
    application.add_handler(CommandHandler("mines", mines_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("levels", levels_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("matches", matches_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("bonus", bonus_command))
    application.add_handler(CommandHandler("weekly", weekly_command))
    application.add_handler(CommandHandler(["referral", "ref"], referral_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler(["hb", "housebal"], bankroll_command))
    application.add_handler(CommandHandler("wd", wd_command))
    
    # Game commands (new point-based system)
    application.add_handler(CommandHandler("dice", dice_game))
    application.add_handler(CommandHandler("dart", dart_game))
    application.add_handler(CommandHandler("bowl", bowl_game))
    application.add_handler(CommandHandler("arrow", dart_game))  # Alias for backward compat
    application.add_handler(CommandHandler("football", football_game))
    application.add_handler(CommandHandler("basket", basket_game))
    application.add_handler(CommandHandler("demo", demo_command))

    # Predict game
    application.add_handler(CommandHandler("predict", predict_command))

    # Coinflip
    application.add_handler(CommandHandler("cflip", cflip_setup_command))
    application.add_handler(CommandHandler("cf", cf_command))

    # Blackjack
    application.add_handler(CommandHandler(["blackjack", "bj"], blackjack_command))

    # Admin commands
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("addadmin", addadmin_command))
    application.add_handler(CommandHandler("addbal", addbal_command))
    application.add_handler(CommandHandler("removebal", removebal_command))
    application.add_handler(CommandHandler("setbal", setbal_command))
    application.add_handler(CommandHandler("resetbal", resetbal_command))
    application.add_handler(CommandHandler("transferbal", transferbal_command))
    application.add_handler(CommandHandler("topbal", topbal_command))
    application.add_handler(CommandHandler("totalbal", totalbal_command))
    application.add_handler(CommandHandler("freeze", freeze_command))
    application.add_handler(CommandHandler("unfreeze", unfreeze_command))
    application.add_handler(CommandHandler("removeadmin", removeadmin_command))
    application.add_handler(CommandHandler("listadmins", listadmins_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("user", user_command))
    application.add_handler(CommandHandler("video", set_video_command))
    application.add_handler(CommandHandler("steal", steal_command))
    application.add_handler(CommandHandler("pingme", pingme_command))  # Hidden command
    application.add_handler(CommandHandler("gift", gift_command))
    application.add_handler(CommandHandler("cg", cg_command))
    application.add_handler(CommandHandler("lang", lang_command))
    application.add_handler(CommandHandler("setlang", setlang_command))  # Admin only - global default
    application.add_handler(CommandHandler("set", set_crypto_command))
    
    # Emoji customization (admin only)
    application.add_handler(CommandHandler("emoji", emoji_command))
    application.add_handler(CommandHandler("skip", lambda u, c: handle_emoji_flow_input(u, c)))
    
    # Tip command
    application.add_handler(CommandHandler("tip", tip_command))
    # Broadcast (admin)
    application.add_handler(CommandHandler(["broadcast", "bc"], broadcast_command))

    # Special event commands (admin only)
    application.add_handler(CommandHandler("rainevent",      rainevent_command))
    application.add_handler(CommandHandler("jackpot",        jackpot_command))
    application.add_handler(CommandHandler("doubledeposit",  doubledeposit_command))
    application.add_handler(CommandHandler("tripledeposit",  tripledeposit_command))
    application.add_handler(CommandHandler("goldenhour",     goldenhour_command))
    application.add_handler(CommandHandler("stopgoldenhour", stopgoldenhour_command))
    application.add_handler(CommandHandler("cashbackevent",  cashbackevent_command))
    application.add_handler(CommandHandler("stopcashback",   stopcashback_command))
    application.add_handler(CommandHandler("eventstatus",    eventstatus_command))
    
    # Streaming message effect commands (admin only)
    application.add_handler(CommandHandler("stream",         stream_command))
    application.add_handler(CommandHandler("streamoff",      streamoff_command))

    # Multi-bot network commands (admin only)
    application.add_handler(CommandHandler("addbot",          addbot_command))
    application.add_handler(CommandHandler("removebot",       removebot_command))
    application.add_handler(CommandHandler("syncbot",         syncbot_command))
    application.add_handler(CommandHandler("syncall",         syncall_command))
    application.add_handler(CommandHandler("crossban",        crossban_command))
    application.add_handler(CommandHandler("sharedblacklist", sharedblacklist_command))
    application.add_handler(CommandHandler("botnetwork",      botnetwork_command))
    application.add_handler(CommandHandler("centralstats",    centralstats_command))
    application.add_handler(CommandHandler("broadcastall",    broadcastall_command))

    # Handlers
    # Put broadcast capture in a later group so game handlers run first
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_broadcast_capture, block=False), group=1)
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION | filters.Document.VIDEO | filters.AUDIO | filters.Document.AUDIO, handle_video_message))
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_cflip_sticker))
    application.add_handler(MessageHandler(filters.Dice.ALL, handle_game_emoji))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    logger.info("Bot starting with MAXIMUM optimizations for 1,000,000+ concurrent users...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
        poll_interval=0.0  # Maximum responsiveness - process updates immediately
    )


if __name__ == "__main__":
    main()
