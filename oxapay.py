# -*- coding: utf-8 -*-
import aiohttp
import hmac
import hashlib
import logging

logger = logging.getLogger(__name__)

SUPPORTED_CURRENCIES = {
    "USDT": {"name": "USDT",     "emoji": "💵", "network": "TRC20"},
    "BTC":  {"name": "Bitcoin",  "emoji": "₿",  "network": "BTC"},
    "ETH":  {"name": "Ethereum", "emoji": "♦️", "network": "ETH"},
    "LTC":  {"name": "Litecoin", "emoji": "Ł",  "network": "LTC"},
    "DOGE": {"name": "Dogecoin", "emoji": "🐶", "network": "DOGE"},
}

class OxaPay:
    def __init__(self, merchant_key):
        self.merchant_key = merchant_key
        self.base_url = "https://api.oxapay.com"

    async def create_deposit_address(self, coin, network, user_id):
        url = f"{self.base_url}/merchants/request/whitelabel"
        payload = {
            "merchant": self.merchant_key,
            "amount": 0.10,
            "currency": "USD",
            "payCurrency": coin,
            "network": network,
            "orderId": str(user_id),
            "lifeTime": 60,
            "description": f"Deposit for user {user_id}"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("result") == 100:
                        return {
                            "address": data.get("address"),
                            "network": data.get("network"),
                            "trackId": str(data.get("trackId"))
                        }
                    else:
                        logger.error(f"OxaPay error: {data}")
                        return None
        except Exception as e:
            logger.error(f"OxaPay create_deposit_address exception: {e}")
            return None

    async def inquiry_deposit(self, trackId):
        url = f"{self.base_url}/merchants/inquiry"
        payload = {
            "merchant": self.merchant_key,
            "trackId": trackId
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                    return data
        except Exception as e:
            logger.error(f"OxaPay inquiry_deposit exception: {e}")
            return None

    async def get_crypto_amount_for_usd(self, usd_amount, currency):
        return round(float(usd_amount), 8)

    async def create_invoice(self, *, amount, currency, user_id=None, **kwargs):
        logger.warning("create_invoice: not used in new system")
        return None

    async def check_invoice(self, track_id):
        return await self.inquiry_deposit(track_id)

    @staticmethod
    def verify_webhook_signature(payload_str, signature, merchant_key):
        calculated = hmac.new(
            merchant_key.encode("utf-8"),
            payload_str.encode("utf-8"),
            hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(calculated, signature)


# Module-level functions so old code calling oxapay.check_invoice() still works
_client = OxaPay(merchant_key="UU7H3W-ONJJG8-ZPEVEL-LATWVM")

async def check_invoice(track_id):
    return await _client.inquiry_deposit(track_id)

async def get_crypto_amount_for_usd(usd_amount, currency):
    return round(float(usd_amount), 8)

async def create_invoice(**kwargs):
    logger.warning("create_invoice stub called")
    return None
