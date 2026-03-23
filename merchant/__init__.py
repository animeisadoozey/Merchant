import logging
from typing import TYPE_CHECKING

from .cog import Merchant

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger(__name__)

async def setup(bot: "BallsDexBot"):
    from ballsdex.core.merchant_models import MerchantItem, merchant_items

    for item in await MerchantItem.all():
        merchant_items[item.pk] = item

    log.info(f"Cached {len(merchant_items)} items.")
    await bot.add_cog(Merchant(bot))
