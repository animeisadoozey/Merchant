import logging
import random
import tomllib
import os
from typing import TYPE_CHECKING
from pathlib import Path
from packaging.version import parse as parse_version

import discord
from discord import app_commands
from discord.ext import commands

from tortoise.exceptions import BaseORMException, DoesNotExist
from tortoise.timezone import now as tortoise_now
from ballsdex.core.currency_models import CurrencySettings, MoneyInstance
from ballsdex.core.merchant_models import MerchantInstance, MerchantItem, MerchantSettings, merchant_items
from ballsdex.core.models import Ball, BallInstance, Player
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import FieldPageSource, Pages
from ballsdex.settings import settings
from ballsdex import __version__ as ballsdex_version

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger(__name__)
SOFT_DELETE_VERSION = parse_version("2.29.5")


class PackageSettings:
    """
    Settings for the Merchant package.
    """
    def __init__(self, path):
        with open(path, "rb") as f:
            data = tomllib.load(f)

        if data is None:
            return

        self.token_ball_name: str = data.get("token_ball_name", "")
        self.token_conversion_rate: int = data.get("token_conversion_rate", 0)

merchant_settings = PackageSettings(Path(os.path.dirname(os.path.abspath(__file__)), "./config.toml"))


class Merchant(commands.GroupCog):
    """
    Merchant commands.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self._currency_settings: CurrencySettings | None = None
        self._merchant_settings: MerchantSettings | None = None
    
    @commands.group(invoke_without_command=True)
    async def merchant(self, ctx: commands.Context):
        """
        Merchant prefix commands.
        """
        await ctx.send_help(ctx.command)
    
    @merchant.command()
    async def reloadcache(self, ctx: commands.Context["BallsDexBot"]):
        """
        Reload the cache of Merchant models.
        """
        merchant_items.clear()
        for merchant in await MerchantItem.all():
            merchant_items[merchant.pk] = merchant
        
        await ctx.message.add_reaction("✅")
    
    @app_commands.command()
    async def shop(self, interaction: discord.Interaction["BallsDexBot"]):
        """
        Check the available items in the shop.
        """
        await interaction.response.defer(thinking=True, ephemeral=True)
        player, _ = await Player.get_or_create(discord_id=interaction.user.id)
        merchant_settings = await self.get_merchant_settings()
        instance, created = await MerchantInstance.get_or_create(
            player=player, 
            defaults={"rotation_ends_at": tortoise_now() + merchant_settings.rotation_delta}
        )
        await instance.fetch_related("items")
        has_items = bool(instance.items)

        if created or not has_items or instance.rotation_expired:
            items = self._get_random_items(merchant_settings.items)
            if not items:
                await interaction.followup.send("Failed to select items for rotation.")
                return
            
            if not created and has_items:
                await instance.items.clear()
            await instance.items.add(*items)

            instance.rotation_ends_at = tortoise_now() + merchant_settings.rotation_delta
            await instance.save(update_fields=("rotation_ends_at",))
        else:
            items = instance.items
        
        entries: list[tuple[str, str]] = [(x.name, await self.format_price(x.prize)) for x in items]
        source = FieldPageSource(entries, per_page=merchant_settings.items, inline=True)
        source.embed.title = f"{settings.bot_name} shop"
        source.embed.description = "Check out your items!\n-# Note: your items are different from other players"

        pages = Pages(source, interaction=interaction, compact=True)
        await pages.start()
    
    @app_commands.command()
    @app_commands.rename(item_id="item")
    async def buy(self, interaction: discord.Interaction["BallsDexBot"], item_id: int):
        """
        Buy an item from the shop.

        Parameters
        ----------
        item_id: int
            The item that you want to
        """
        currency_settings = await self.get_curreny_settings()
        try:
            item = await MerchantItem.get(pk=item_id)
        except DoesNotExist:
            await interaction.response.send_message(
                f"Item with id `#{item_id:0X}` doesn't exist.", 
                ephemeral=True,
            )
            return
        try:
            player = await Player.get(discord_id=interaction.user.id)
            money_instance = await MoneyInstance.get(player=player)
            instance = await MerchantInstance.get(player=player)
        except DoesNotExist:
            await interaction.response.send_message("You're not registred in the economy system yet.")
            return
    
        has_item = await instance.items.filter(pk=item_id).exists()
        if not has_item:
            await interaction.response.send_message(
                "You can't buy this item because it's not in your current selection.",
                ephemeral=True,
            )
            return
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        if not item.prize:
            instance = await BallInstance.create(
                player=player,
                ball=item.cached_ball,
                special=item.cached_special,
                health_bonus=random.randint(-settings.max_health_bonus, settings.max_health_bonus),
                attack_bonus=random.randint(-settings.max_attack_bonus, settings.max_attack_bonus),
                catch_date=tortoise_now(),
                server_id=interaction.guild_id,
            )
            await interaction.followup.send(
                f"You've bought {item.name} for **free!**\n"
                f"{instance.description(include_emoji=True, bot=self.bot)}"
            )
            return

        if money_instance.amount < item.prize:
            currency_emoji = (
                self.bot.get_emoji(currency_settings.emoji_id) 
                if currency_settings.emoji_id 
                else ""
            )
            await interaction.followup.send(
                f"You don't enough {currency_emoji} {currency_settings.name} to buy "
                f"**{item.name}**\n"
                f"Your actual balance: {await self.format_price(money_instance.amount)}"
            )
            return

        try:
            instance = await BallInstance.create(
                player=player,
                ball=item.cached_ball,
                special=item.cached_special,
                health_bonus=random.randint(-settings.max_health_bonus, settings.max_health_bonus),
                attack_bonus=random.randint(-settings.max_attack_bonus, settings.max_attack_bonus),
                catch_date=tortoise_now(),
                server_id=interaction.guild_id,
            )
        except BaseORMException:
            log.exception("Failed to create a ball instance while a user trying to buy an item.", exc_info=True)
            await interaction.followup.send("An error occurred while trying to buy the item.")
            return
        else:
            money_instance.amount -= item.prize
            await money_instance.save(update_fields=("amount",))
            await interaction.followup.send(
                f"You've bought {item.name} for **{await self.format_price(item.prize)}!**\n"
                f"{instance.description(include_emoji=True, bot=self.bot)}"
            )
            return
    
    @app_commands.command()
    async def convert_token(
        self, 
        interaction: discord.Interaction["BallsDexBot"], 
        amount: int = 1
    ):
        """
        Convert a token into coins.

        Parameters
        ----------
        amount: int
            Number of tokens to convert.
        """
        if not merchant_settings.token_ball_name:
            await interaction.response.send_message("This command isn't configured yet.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("Please select a valid amount.", ephemeral=True)
            return
        
        try:
            token_ball = await Ball.get(country=merchant_settings.token_ball_name)
        except DoesNotExist:
            log.exception(f"{merchant_settings.token_ball_name} doesn't exist as a ball.")
            await interaction.response.send_message(
                f"An error occurred while trying to fetch token {settings.collectible_name}.",
                ephemeral=True,
            )
            return
        
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        instances = (
            BallInstance
            .filter(special_id__isnull=True, ball_id=token_ball.pk)
            .order_by("-catch_date")
            .limit(amount)
        )
        if await instances.count() < amount:
            await interaction.followup.send(
                f"You can't convert **{amount} tokens** because you don't have that amount."
            )
            return

        currency_settings = await self.get_curreny_settings()
        grammar = "token" if amount == 1 else "tokens"
        view = ConfirmChoiceView(
            interaction, 
            accept_message=f"Confirmed, converting {amount} {grammar}...",
            cancel_message="Request cancelled.",
        )
        await interaction.followup.send(
            f"Are you sure you want to convert **{amount} {grammar}** into "
            f"**{await self.format_price(merchant_settings.token_conversion_rate * amount)}**?",
            view=view,
            ephemeral=True
        )
        await view.wait()
        if not view.value:
            return
        
        ids = await instances.values_list("id", flat=True)
        if parse_version(ballsdex_version) >= SOFT_DELETE_VERSION:
            await BallInstance.filter(id__in=ids).update(deleted=True)
        else:
            await BallInstance.filter(id__in=ids).delete()
        
        money_instance, created = await MoneyInstance.get_or_create(
            player__discord_id=interaction.user.id,
            defaults={"amount": merchant_settings.token_conversion_rate * amount}
        )
        if not created:
            money_instance.amount += merchant_settings.token_conversion_rate * amount
            await money_instance.save(update_fields=("amount",))

        await interaction.followup.send(
            f"Converted! All tokens successfully converted into {currency_settings.plural_name}.\n"
            f"Converted tokens: **{amount}**\n"
            f"Given amount: **{await self.format_price(merchant_settings.token_conversion_rate * amount)}**\n"
            f"Actual balance: **{await self.format_price(money_instance.amount)}**",
            ephemeral=True
        )

    @buy.autocomplete("item_id")
    async def item_autocomplete(
        self, 
        interaction: discord.Interaction["BallsDexBot"], 
        current: str
    ) -> list[app_commands.Choice[int]]:
        player, _ = await Player.get_or_create(discord_id=interaction.user.id)
        merchant_settings = await self.get_merchant_settings()
        instance, created = await MerchantInstance.get_or_create(
            player=player, 
            defaults={"rotation_ends_at": tortoise_now() + merchant_settings.rotation_delta}
        )
        await instance.fetch_related("items")
        has_items = bool(instance.items)

        if created or not has_items or instance.rotation_expired:
            items = self._get_random_items(merchant_settings.items)
            if not items:
                return []
            
            if not created and has_items:
                await instance.items.clear()
            await instance.items.add(*items)

            instance.rotation_ends_at = tortoise_now() + merchant_settings.rotation_delta
            await instance.save(update_fields=("rotation_ends_at",))
        else:
            items = instance.items
        
        return [
            app_commands.Choice(name=f"#{x.pk:0X} {x.name} ({await self.format_price(x.prize, False)})", value=x.pk) 
            for x in items
            if current.lower() in x.name.lower()
        ]
    
    async def format_price(self, amount: int | None, include_emoji: bool = True):
        currency_settings = await self.get_curreny_settings()
        text = f"{amount:,} {currency_settings.display_name(amount)}" if amount else "Free"
        if include_emoji:
            emoji = self.bot.get_emoji(currency_settings.emoji_id)
            if emoji:
                text = f"{emoji} {text}"
        return text

    def _get_random_items(self, amount: int) -> list[MerchantItem] | None:
        population = [x for x in merchant_items.values() if x.enabled]

        if not population:
            return None
        
        amount = min(amount, len(population))
        selected = []

        for _ in range(amount):
            weights = [x.rarity for x in population]
            choice = random.choices(population, weights=weights, k=1)[0]
            selected.append(choice)
            population.remove(choice)
        
        return selected

    async def get_curreny_settings(self, refresh: bool = True):
        if not self._currency_settings:
            self._currency_settings = await CurrencySettings.load()
        if refresh:
            await self._currency_settings.refresh_from_db()
        return self._currency_settings

    async def get_merchant_settings(self, refresh: bool = True):
        if not self._merchant_settings:
            self._merchant_settings = await MerchantSettings.load()
        if refresh:
            await self._merchant_settings.refresh_from_db()
        return self._merchant_settings
