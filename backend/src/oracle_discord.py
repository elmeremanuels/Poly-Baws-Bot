"""Oracle Discord Bot — trade alerts, confirmations, and slash commands for Poly-Baws-Bot.

Runs as an asyncio task alongside the main trading bot. All send functions are safe
to call even when Discord is not configured (empty token → silently skip).
No circular imports: local module imports are lazy (inside functions).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands

from .config_loader import CONFIG
from .logger import log


# ── Module-level state ────────────────────────────────────────────────────────

_bot: commands.Bot | None = None
_channel: discord.TextChannel | None = None          # trade alerts channel
_confirm_channel: discord.TextChannel | None = None  # confirm questions channel
_ready = asyncio.Event()


# ── Helpers ───────────────────────────────────────────────────────────────────

_COIN_EMOJI: dict[str, str] = {
    "BTC": "₿",
    "ETH": "Ξ",
    "SOL": "◎",
    "DOGE": "Ð",
    "XRP": "✕",
}

_COLORS = {
    "green": 0x2ECC71,
    "red": 0xE74C3C,
    "yellow": 0xF1C40F,
    "blue": 0x3498DB,
    "orange": 0xE67E22,
    "grey": 0x95A5A6,
}


def _coin_label(coin: str) -> str:
    emoji = _COIN_EMOJI.get(coin.upper(), "")
    return f"{emoji} {coin}" if emoji else coin


def _temperature_label(temperature: int) -> str:
    """Return emoji + Dutch label for a trading temperature value."""
    if temperature < 30:
        return f"🔵 ijskoud ({temperature})"
    if temperature < 50:
        return f"🟡 koud ({temperature})"
    if temperature < 70:
        return f"🟠 lauw ({temperature})"
    if temperature < 85:
        return f"🟢 warm ({temperature})"
    return f"✅ heet ({temperature})"


def _temperature_color(temperature: int) -> int:
    if temperature < 30:
        return _COLORS["blue"]
    if temperature < 50:
        return _COLORS["yellow"]
    if temperature < 70:
        return _COLORS["orange"]
    if temperature < 85:
        return _COLORS["green"]
    return 0x27AE60  # bright green for "heet"


def _pnl_sign(value: float) -> str:
    return f"+{value:.2f}" if value >= 0 else f"{value:.2f}"


def _oracle_cfg() -> dict:
    return CONFIG.get("oracle", {})


def _get_token() -> str:
    return _oracle_cfg().get("discord_token", "") or ""


def _get_channel_id() -> int | None:
    raw = _oracle_cfg().get("discord_channel_id")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _get_confirm_channel_id() -> int | None:
    raw = _oracle_cfg().get("discord_confirm_channel_id")
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


# ── OracleConfirmView ─────────────────────────────────────────────────────────

class OracleConfirmView(discord.ui.View):
    """Interactive Yes/No buttons for Oracle trade confirmations."""

    def __init__(self) -> None:
        super().__init__(timeout=None)  # timeout managed externally via wait_for_response
        self._response_event: asyncio.Event = asyncio.Event()
        self._approved: bool | None = None

    @discord.ui.button(label="✅ Ja, doorgaan", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._approved = True
        self._response_event.set()
        self.disable_all_items()
        await interaction.response.edit_message(
            content="✅ **Goedgekeurd** — trade gaat door.",
            view=self,
        )

    @discord.ui.button(label="🚫 Nee, skip", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._approved = False
        self._response_event.set()
        self.disable_all_items()
        await interaction.response.edit_message(
            content="🚫 **Afgewezen** — trade overgeslagen.",
            view=self,
        )

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def wait_for_response(self, timeout: int) -> bool:
        """Wait up to *timeout* seconds. Returns True=approve, False=reject (default approve on timeout)."""
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=float(timeout))
            return bool(self._approved)
        except asyncio.TimeoutError:
            self.disable_all_items()
            return True  # default approve so bot doesn't freeze


# ── Bot startup ───────────────────────────────────────────────────────────────

async def start_discord_bot() -> None:
    """Start the Discord bot. Called once from bot.py as asyncio.create_task(start_discord_bot())."""
    global _bot, _channel, _confirm_channel

    token = _get_token()
    if not token:
        log.info("discord_bot_skipped", reason="no_token_configured")
        return

    intents = discord.Intents.default()
    intents.message_content = False  # we only use slash commands and button interactions

    _bot = commands.Bot(command_prefix="!", intents=intents)
    _register_slash_commands(_bot)

    @_bot.event
    async def on_ready() -> None:
        global _channel, _confirm_channel
        log.info("discord_bot_ready", user=str(_bot.user))

        channel_id = _get_channel_id()
        if channel_id:
            ch = _bot.get_channel(channel_id)
            if isinstance(ch, discord.TextChannel):
                _channel = ch
                log.info("discord_alerts_channel_set", channel=ch.name)
            else:
                log.warning("discord_alerts_channel_not_found", channel_id=channel_id)

        confirm_id = _get_confirm_channel_id()
        if confirm_id:
            ch2 = _bot.get_channel(confirm_id)
            if isinstance(ch2, discord.TextChannel):
                _confirm_channel = ch2
                log.info("discord_confirm_channel_set", channel=ch2.name)
            else:
                log.warning("discord_confirm_channel_not_found", channel_id=confirm_id)

        try:
            synced = await _bot.tree.sync()
            log.info("discord_commands_synced", count=len(synced))
        except Exception as exc:
            log.warning("discord_commands_sync_failed", error=str(exc))

        _ready.set()

    @_bot.event
    async def on_error(event: str, *args, **kwargs) -> None:
        log.warning("discord_event_error", event=event)

    try:
        await _bot.start(token)
    except discord.LoginFailure as exc:
        log.error("discord_login_failed", error=str(exc))
    except Exception as exc:
        log.error("discord_bot_error", error=str(exc))


# ── Send functions ────────────────────────────────────────────────────────────

async def send_trade_alert(
    coin: str,
    side: str,
    conviction: float,
    regime: str,
    temperature: int,
    trade_id: str,
    paper: bool,
) -> None:
    """Push trade entry alert to the alerts channel with emoji thermometer."""
    if _channel is None:
        return
    try:
        mode_tag = "📄 PAPER" if paper else "💰 LIVE"
        color = _COLORS["yellow"] if paper else _temperature_color(temperature)
        embed = discord.Embed(
            title=f"🚀 Trade Entry — {_coin_label(coin)}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Coin", value=_coin_label(coin), inline=True)
        embed.add_field(name="Side", value=side.upper(), inline=True)
        embed.add_field(name="Mode", value=mode_tag, inline=True)
        embed.add_field(name="Conviction", value=f"{conviction:.0%}", inline=True)
        embed.add_field(name="Regime", value=regime, inline=True)
        embed.add_field(name="Temperatuur", value=_temperature_label(temperature), inline=True)
        embed.set_footer(text=f"Trade ID: {trade_id}")
        await _channel.send(embed=embed)
    except Exception as exc:
        log.warning("discord_send_trade_alert_failed", error=str(exc))


async def send_trade_outcome(
    trade_id: str,
    coin: str,
    net_pnl: float,
    won: bool,
    oracle_correct: bool | None,
    temperature: int,
) -> None:
    """Push trade close result to the alerts channel."""
    if _channel is None:
        return
    try:
        result_emoji = "✅ WIN" if won else "❌ LOSS"
        color = _COLORS["green"] if won else _COLORS["red"]
        embed = discord.Embed(
            title=f"{result_emoji} — {_coin_label(coin)}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Net P&L", value=f"**{_pnl_sign(net_pnl)} USDC**", inline=True)
        embed.add_field(name="Temperatuur", value=_temperature_label(temperature), inline=True)
        if oracle_correct is not None:
            oracle_tag = "✅ Correct" if oracle_correct else "❌ Fout"
            embed.add_field(name="Oracle Voorspelling", value=oracle_tag, inline=True)
        embed.set_footer(text=f"Trade ID: {trade_id}")
        await _channel.send(embed=embed)
    except Exception as exc:
        log.warning("discord_send_trade_outcome_failed", error=str(exc))


async def send_oracle_confirm(
    coin: str,
    trade_id: str,
    reason: str,
    temperature: int,
    timeout_secs: int = 90,
) -> bool:
    """Send an interactive confirm to confirm_channel.

    Returns True=approve, False=reject. On timeout returns True (default approve).
    """
    channel = _confirm_channel or _channel
    if channel is None:
        return True  # silently approve when no channel configured

    try:
        embed = discord.Embed(
            title=f"🔔 Oracle Bevestiging — {_coin_label(coin)}",
            description=reason,
            color=_temperature_color(temperature),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Coin", value=_coin_label(coin), inline=True)
        embed.add_field(name="Temperatuur", value=_temperature_label(temperature), inline=True)
        embed.add_field(name="Timeout", value=f"{timeout_secs}s", inline=True)
        embed.set_footer(text=f"Trade ID: {trade_id}")

        view = OracleConfirmView()
        await channel.send(embed=embed, view=view)
        result = await view.wait_for_response(timeout_secs)
        log.info(
            "discord_oracle_confirm_result",
            coin=coin,
            trade_id=trade_id,
            approved=result,
        )
        return result
    except Exception as exc:
        log.warning("discord_send_oracle_confirm_failed", error=str(exc))
        return True  # fail open — never block a trade on Discord errors


async def send_daily_analysis(analysis: dict) -> None:
    """Send 2x daily oracle analysis embed to the alerts channel."""
    if _channel is None:
        return
    try:
        outlook = analysis.get("outlook", "onbekend")
        risk_level = analysis.get("risk_level", "onbekend")
        discord_summary = analysis.get("discord_summary", "")
        reasoning = analysis.get("reasoning", "")
        fg_value = analysis.get("fear_greed_at_time")
        coin_sentiments = analysis.get("coin_sentiments", {})
        suggested = analysis.get("suggested_adjustments", {})

        # Color based on outlook
        outlook_lower = str(outlook).lower()
        if "bullish" in outlook_lower or "positief" in outlook_lower:
            color = _COLORS["green"]
        elif "bearish" in outlook_lower or "negatief" in outlook_lower:
            color = _COLORS["red"]
        else:
            color = _COLORS["yellow"]

        embed = discord.Embed(
            title="📊 Oracle Dagelijkse Analyse",
            description=discord_summary or reasoning[:500] if reasoning else "Geen samenvatting beschikbaar.",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Outlook", value=outlook, inline=True)
        embed.add_field(name="Risico niveau", value=risk_level, inline=True)
        if fg_value is not None:
            embed.add_field(name="Fear & Greed", value=str(fg_value), inline=True)

        if coin_sentiments and isinstance(coin_sentiments, dict):
            lines = [f"**{c}**: {s}" for c, s in list(coin_sentiments.items())[:6]]
            embed.add_field(
                name="Coin Sentimenten",
                value="\n".join(lines) or "–",
                inline=False,
            )

        if suggested and isinstance(suggested, dict):
            adj_lines = [f"• **{k}**: {v}" for k, v in list(suggested.items())[:5]]
            embed.add_field(
                name="Aanbevolen aanpassingen",
                value="\n".join(adj_lines) or "–",
                inline=False,
            )

        await _channel.send(embed=embed)
    except Exception as exc:
        log.warning("discord_send_daily_analysis_failed", error=str(exc))


async def send_status_update(message: str) -> None:
    """Send a generic status message to the alerts channel."""
    if _channel is None:
        return
    try:
        embed = discord.Embed(
            description=f"ℹ️ {message}",
            color=_COLORS["grey"],
            timestamp=datetime.now(timezone.utc),
        )
        await _channel.send(embed=embed)
    except Exception as exc:
        log.warning("discord_send_status_update_failed", error=str(exc))


async def send_mid_trade_alert(
    trade_id: str,
    coin: str,
    side: str,
    cur_mid: float,
    secs_rem: float,
    alert_type: str,
) -> None:
    """Alert during active trade (take_it triggered, save_it triggered, price warning)."""
    if _channel is None:
        return
    try:
        type_map = {
            "take_it": ("💰 Take It — vroeg uitstappen", _COLORS["green"]),
            "save_it": ("🛡️ Save It — verlies beperken", _COLORS["orange"]),
            "price_warning": ("⚠️ Prijs waarschuwing", _COLORS["yellow"]),
        }
        title, color = type_map.get(
            alert_type,
            (f"⚡ {alert_type}", _COLORS["grey"]),
        )

        embed = discord.Embed(
            title=f"{title} — {_coin_label(coin)}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Side", value=side.upper(), inline=True)
        embed.add_field(name="Huidige mid", value=f"{cur_mid:.4f}", inline=True)
        embed.add_field(name="Resterende tijd", value=f"{int(secs_rem)}s", inline=True)
        embed.set_footer(text=f"Trade ID: {trade_id}")
        await _channel.send(embed=embed)
    except Exception as exc:
        log.warning("discord_send_mid_trade_alert_failed", error=str(exc))


# ── Resume confirm view ───────────────────────────────────────────────────────

class _ResumeConfirmView(discord.ui.View):
    """One-shot confirm button for /resume command."""

    def __init__(self) -> None:
        super().__init__(timeout=60)
        self._confirmed = asyncio.Event()

    @discord.ui.button(label="✅ Bevestig — Ga live", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._confirmed.set()
        button.disabled = True
        await interaction.response.edit_message(
            content="✅ Live modus geactiveerd.",
            view=self,
        )

    async def wait(self) -> bool:
        try:
            await asyncio.wait_for(self._confirmed.wait(), timeout=60)
            return True
        except asyncio.TimeoutError:
            return False


# ── Slash commands ────────────────────────────────────────────────────────────

def _register_slash_commands(bot: commands.Bot) -> None:
    """Register all slash commands on the bot tree."""

    @bot.tree.command(name="status", description="Toon de huidige bot-status en P&L")
    async def cmd_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .state import get_mode, get_active_trades
            from .db_sync import get_daily_pnl, get_state

            mode = get_mode()
            daily_pnl = get_daily_pnl()
            open_count = len(get_active_trades())

            track_record_raw = get_state("oracle_track_record") or "–"
            fg_raw = get_state("oracle_fear_greed_value") or "–"
            fg_label = get_state("oracle_fear_greed_label") or ""
            temp_raw = get_state("oracle_trading_temperature")
            temperature = int(float(temp_raw)) if temp_raw else 0

            embed = discord.Embed(
                title="🤖 Poly-Baws-Bot Status",
                color=_temperature_color(temperature),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Mode", value=mode, inline=True)
            embed.add_field(name="Open trades", value=str(open_count), inline=True)
            embed.add_field(name="Vandaag P&L", value=f"{_pnl_sign(daily_pnl)} USDC", inline=True)
            embed.add_field(name="Temperatuur", value=_temperature_label(temperature), inline=True)
            embed.add_field(
                name="Fear & Greed",
                value=f"{fg_raw} ({fg_label})" if fg_label else str(fg_raw),
                inline=True,
            )
            embed.add_field(name="Oracle Track Record", value=str(track_record_raw), inline=True)
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_status_failed", error=str(exc))
            await interaction.followup.send(f"❌ Fout bij ophalen status: {exc}", ephemeral=True)

    @bot.tree.command(name="pause", description="Schakel naar paper modus (pauzeer live trading)")
    async def cmd_pause(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .state import get_mode, set_mode
            prev = get_mode()
            if prev.startswith("paper"):
                await interaction.followup.send("ℹ️ Bot staat al in paper modus.", ephemeral=True)
                return
            new_mode = "paper_auto" if prev.endswith("auto") else "paper_hybrid"
            set_mode(new_mode)
            log.info("discord_pause_command", prev_mode=prev, new_mode=new_mode)
            embed = discord.Embed(
                title="⏸️ Bot gepauzeerd",
                description=f"Modus gewisseld van **{prev}** → **{new_mode}**\nLive trading is gestopt.",
                color=_COLORS["yellow"],
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_pause_failed", error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(name="resume", description="Zet bot terug naar live modus (vraagt bevestiging)")
    async def cmd_resume(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .state import get_mode, set_mode
            current = get_mode()
            if not current.startswith("paper"):
                await interaction.followup.send("ℹ️ Bot staat al in live modus.", ephemeral=True)
                return

            view = _ResumeConfirmView()
            await interaction.followup.send(
                "⚠️ **Weet je zeker dat je naar live modus wilt?** Klik ter bevestiging:",
                view=view,
            )
            confirmed = await view.wait()
            if confirmed:
                new_mode = "live_auto" if current.endswith("auto") else "live_hybrid"
                set_mode(new_mode)
                log.info("discord_resume_command", prev_mode=current, new_mode=new_mode)
            else:
                log.info("discord_resume_cancelled")
        except Exception as exc:
            log.warning("discord_cmd_resume_failed", error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(name="stop_coin", description="Schakel een specifieke coin uit via coin_guard")
    @app_commands.describe(coin="De coin om uit te schakelen (bijv. BTC)")
    async def cmd_stop_coin(interaction: discord.Interaction, coin: str) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from . import coin_guard as _cg
            coin_upper = coin.upper()
            # Force-disable by directly setting disabled state
            await _cg._do_disable(coin_upper, f"Handmatig uitgeschakeld via Discord /stop_coin door {interaction.user}")
            log.info("discord_stop_coin", coin=coin_upper, user=str(interaction.user))
            embed = discord.Embed(
                title=f"🚫 Coin uitgeschakeld: {_coin_label(coin_upper)}",
                description=f"**{coin_upper}** is uitgeschakeld via coin_guard.",
                color=_COLORS["red"],
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_stop_coin_failed", coin=coin, error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(name="enable_coin", description="Heractiveer een uitgeschakelde coin")
    @app_commands.describe(coin="De coin om te heractiveren (bijv. BTC)")
    async def cmd_enable_coin(interaction: discord.Interaction, coin: str) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from . import coin_guard as _cg
            coin_upper = coin.upper()
            await _cg.enable_coin(coin_upper)
            log.info("discord_enable_coin", coin=coin_upper, user=str(interaction.user))
            embed = discord.Embed(
                title=f"✅ Coin heractiveerd: {_coin_label(coin_upper)}",
                description=f"**{coin_upper}** is weer ingeschakeld.",
                color=_COLORS["green"],
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_enable_coin_failed", coin=coin, error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(name="analyse", description="Start onmiddellijk een Oracle dagelijkse analyse")
    async def cmd_analyse(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from . import oracle as _oracle
            await interaction.followup.send("⏳ Oracle analyse wordt gestart...", ephemeral=False)
            snap = await _oracle.run_daily_analysis()
            if snap:
                await send_daily_analysis(snap)
            else:
                await send_status_update("Oracle analyse voltooid (geen output beschikbaar).")
            log.info("discord_cmd_analyse", user=str(interaction.user))
        except Exception as exc:
            log.warning("discord_cmd_analyse_failed", error=str(exc))
            await interaction.followup.send(f"❌ Fout bij analyse: {exc}", ephemeral=True)

    @bot.tree.command(name="oracle", description="Zet de Oracle hard_gate aan of uit")
    @app_commands.describe(state="'on' om te activeren, 'off' om te deactiveren")
    @app_commands.choices(state=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    async def cmd_oracle(interaction: discord.Interaction, state: str) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            oracle_cfg = CONFIG.setdefault("oracle", {})
            enabled = (state == "on")
            oracle_cfg["hard_gate"] = enabled
            status_word = "geactiveerd ✅" if enabled else "gedeactiveerd 🚫"
            log.info("discord_oracle_toggle", hard_gate=enabled, user=str(interaction.user))
            embed = discord.Embed(
                title=f"Oracle hard_gate {status_word}",
                description=f"Oracle hard_gate is nu **{'aan' if enabled else 'uit'}**.",
                color=_COLORS["green"] if enabled else _COLORS["red"],
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_oracle_failed", error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(name="abort_trade", description="Markeer een trade als afgebroken")
    @app_commands.describe(trade_id="Het trade ID om te annuleren")
    async def cmd_abort_trade(interaction: discord.Interaction, trade_id: str) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .state import update_trade_field, remove_active_trade, get_active_trades
            from .logger import update_trade as _db_update_trade

            active = get_active_trades()
            if trade_id not in active:
                await interaction.followup.send(
                    f"⚠️ Trade **{trade_id}** niet gevonden in actieve trades.", ephemeral=True
                )
                return

            trade = active[trade_id]
            coin = trade.get("coin", "–")
            update_trade_field(trade_id, "status", "aborted")
            await _db_update_trade(trade_id, {"status": "aborted"})
            remove_active_trade(trade_id)
            log.info("discord_abort_trade", trade_id=trade_id, coin=coin, user=str(interaction.user))
            embed = discord.Embed(
                title=f"🛑 Trade afgebroken — {_coin_label(coin)}",
                description=f"Trade `{trade_id}` is gemarkeerd als **aborted** en verwijderd uit actieve trades.",
                color=_COLORS["red"],
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_abort_trade_failed", trade_id=trade_id, error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)
