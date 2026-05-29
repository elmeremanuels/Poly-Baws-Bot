"""Oracle Discord bot — push alerts, interactive confirms, slash commands.

Setup (eenmalig):
  1. discord.com/developers → New Application → Bot → kopieer token
  2. Zet DISCORD_BOT_TOKEN in backend/config/.env
  3. Zet discord_channel_id + discord_confirm_channel_id in config.yaml
  4. Bot uitnodigen: OAuth2 → URL Generator → scopes: bot + applications.commands
     Permissions: Send Messages, Embed Links, Use Slash Commands
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands

from .config_loader import CONFIG
from .logger import log


# ── Module-level state ────────────────────────────────────────────────────────

_bot: commands.Bot | None = None
_channel: discord.TextChannel | None = None
_confirm_channel: discord.TextChannel | None = None
_ready = asyncio.Event()

COIN_EMOJI = {"BTC": "₿", "ETH": "Ξ", "SOL": "◎", "DOGE": "Ð", "XRP": "✕"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _temp_display(temp: int) -> str:
    """Return emoji + Dutch label for a trading temperature value (0–100)."""
    if temp <= 29:
        return "🔵 IJskoud"
    if temp <= 49:
        return "🟡 Koud"
    if temp <= 69:
        return "🟠 Lauw"
    if temp <= 84:
        return "🟢 Warm"
    return "✅ Heet"


def _temp_color(temp: int) -> int:
    """Return Discord embed colour integer for a trading temperature."""
    if temp < 30:
        return 0x3B82F6   # blue
    if temp < 50:
        return 0xEAB308   # yellow
    if temp < 70:
        return 0xF97316   # orange
    if temp < 85:
        return 0x22C55E   # green
    return 0x16A34A        # dark green


def _coin_label(coin: str) -> str:
    emoji = COIN_EMOJI.get(coin.upper(), "")
    return f"{emoji} {coin}" if emoji else coin


def _pnl_sign(value: float) -> str:
    return f"+{value:.2f}" if value >= 0 else f"{value:.2f}"


def _oracle_cfg() -> dict:
    return CONFIG.get("oracle", {})


# ── OracleConfirmView ─────────────────────────────────────────────────────────

class OracleConfirmView(discord.ui.View):
    """Interactive Yes/No buttons for Oracle trade confirmations."""

    def __init__(self, timeout: float = 90) -> None:
        super().__init__(timeout=timeout)
        self._approved: bool = True   # default approve on timeout
        self._event = asyncio.Event()

    @discord.ui.button(label="✅ Ja, doorgaan", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._approved = True
        self._event.set()
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(content="✅ Trade goedgekeurd.", view=self)

    @discord.ui.button(label="🚫 Nee, skip", style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._approved = False
        self._event.set()
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(content="🚫 Trade overgeslagen.", view=self)

    async def on_timeout(self) -> None:
        self._event.set()  # _approved stays True (default approve)

    async def wait_for_response(self) -> bool:
        """Wait until a button is pressed or timeout fires. Returns approval bool."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            pass
        return self._approved


# ── Resume confirm view ───────────────────────────────────────────────────────

class _ResumeConfirmView(discord.ui.View):
    """One-shot confirmation button for /resume command."""

    def __init__(self) -> None:
        super().__init__(timeout=60)
        self._confirmed = asyncio.Event()

    @discord.ui.button(label="✅ Bevestig — Ga live", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._confirmed.set()
        button.disabled = True
        await interaction.response.edit_message(content="✅ Live modus geactiveerd.", view=self)

    async def wait(self) -> bool:
        try:
            await asyncio.wait_for(self._confirmed.wait(), timeout=60)
            return True
        except asyncio.TimeoutError:
            return False


# ── Bot startup ───────────────────────────────────────────────────────────────

async def start_discord_bot() -> None:
    """Start the Discord bot. No-op if token not set."""
    global _bot

    cfg = _oracle_cfg()
    token = os.environ.get("DISCORD_BOT_TOKEN") or cfg.get("discord_bot_token", "")
    if not token:
        log.info("oracle_discord_no_token")
        return

    intents = discord.Intents.default()
    intents.message_content = False

    _bot = commands.Bot(command_prefix="!", intents=intents)

    @_bot.event
    async def on_ready() -> None:
        global _channel, _confirm_channel
        channel_id = int(cfg.get("discord_channel_id") or 0)
        confirm_id = int(cfg.get("discord_confirm_channel_id") or 0)
        if channel_id:
            ch = _bot.get_channel(channel_id)
            if isinstance(ch, discord.TextChannel):
                _channel = ch
                log.info("oracle_discord_alerts_channel_set", channel=ch.name)
            else:
                log.warning("oracle_discord_alerts_channel_not_found", channel_id=channel_id)
        if confirm_id:
            ch2 = _bot.get_channel(confirm_id)
            if isinstance(ch2, discord.TextChannel):
                _confirm_channel = ch2
                log.info("oracle_discord_confirm_channel_set", channel=ch2.name)
            else:
                log.warning("oracle_discord_confirm_channel_not_found", channel_id=confirm_id)
        try:
            synced = await _bot.tree.sync()
            log.info("oracle_discord_commands_synced", count=len(synced))
        except Exception as e:
            log.warning("discord_sync_error", error=str(e))
        _ready.set()
        log.info(
            "oracle_discord_ready",
            channel=str(_channel),
            confirm=str(_confirm_channel),
        )

    @_bot.event
    async def on_error(event: str, *args, **kwargs) -> None:
        log.warning("discord_event_error", event=event)

    # Register slash commands before starting
    _register_slash_commands(_bot)

    try:
        await _bot.start(token)
    except discord.LoginFailure as exc:
        log.error("oracle_discord_login_failed", error=str(exc))
    except Exception as exc:
        log.error("oracle_discord_error", error=str(exc))


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
    """Push trade entry alert to the alerts channel."""
    if _channel is None:
        return
    try:
        emoji = COIN_EMOJI.get(coin.upper(), "")
        title = f"{emoji} {coin} — {'📄 Paper' if paper else '💸 Live'} Trade"
        color = 0x22C55E if side.upper() == "YES" else 0xEF4444
        arrow = "⬆️" if side.upper() == "YES" else "⬇️"
        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Richting", value=f"{side.upper()} {arrow}", inline=True)
        embed.add_field(name="Conviction", value=f"{conviction:.2f}", inline=True)
        embed.add_field(name="Regime", value=regime, inline=True)
        embed.add_field(
            name="Temperatuur",
            value=f"{_temp_display(temperature)} ({temperature})",
            inline=True,
        )
        embed.add_field(name="Trade ID", value=trade_id[:8], inline=True)
        embed.set_footer(text=f"Tijdstip UTC: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
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
        result_icon = "✅" if won else "❌"
        result_word = "Gewonnen" if won else "Verloren"
        color = 0x22C55E if won else 0xEF4444
        if oracle_correct is True:
            correct_str = "✓"
        elif oracle_correct is False:
            correct_str = "✗"
        else:
            correct_str = "—"
        embed = discord.Embed(
            title=f"{result_icon} {coin} — {result_word}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="P&L", value=f"€{net_pnl:+.2f}", inline=True)
        embed.add_field(name="Oracle correct?", value=correct_str, inline=True)
        embed.add_field(
            name="Temperatuur",
            value=f"{_temp_display(temperature)} ({temperature})",
            inline=True,
        )
        embed.set_footer(text=f"Trade ID: {trade_id[:8]}")
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
    """Send interactive confirm to confirm_channel (fallback: _channel).

    Returns True=approve, False=reject. On timeout returns True (default approve).
    """
    channel = _confirm_channel or _channel
    if channel is None:
        return True  # silently approve when no channel configured

    try:
        view = OracleConfirmView(timeout=float(timeout_secs))
        embed = discord.Embed(
            title=f"🤔 Oracle wil bevestiging — {coin}",
            description=(
                f"Temperatuur is {temperature} ({_temp_display(temperature)})\n"
                f"Reden: `{reason}`\n"
                f"Automatic approval in {timeout_secs}s."
            ),
            color=_temp_color(temperature),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Coin", value=_coin_label(coin), inline=True)
        embed.add_field(name="Trade ID", value=trade_id[:8], inline=True)
        embed.add_field(name="Timeout", value=f"{timeout_secs}s", inline=True)
        await channel.send(embed=embed, view=view)
        result = await view.wait_for_response()
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
    """Send Oracle daily analysis embed to the alerts channel."""
    if _channel is None:
        return
    try:
        outlook = analysis.get("outlook", "onbekend")
        risk_level = analysis.get("risk_level", "onbekend")
        discord_summary = analysis.get("discord_summary", "")
        pattern_insights = analysis.get("pattern_insights", "")
        suggested = analysis.get("suggested_adjustments", {})
        fg_value = analysis.get("fear_greed_at_time")

        outlook_upper = str(outlook).upper()
        if "BULLISH" in outlook_upper:
            color = 0x22C55E
        elif "BEARISH" in outlook_upper:
            color = 0xEF4444
        else:
            color = 0x6B7280  # grey

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        embed = discord.Embed(
            title=f"📊 Oracle Dagelijkse Analyse — {now_str}",
            description=discord_summary or "Geen samenvatting beschikbaar.",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Outlook", value=str(outlook), inline=True)
        embed.add_field(name="Risk Level", value=str(risk_level), inline=True)
        if fg_value is not None:
            embed.add_field(name="Fear & Greed", value=str(fg_value), inline=True)
        if discord_summary:
            embed.add_field(
                name="Discord Summary",
                value=discord_summary[:1024],
                inline=False,
            )
        if pattern_insights:
            embed.add_field(
                name="Pattern Insights",
                value=str(pattern_insights)[:1024],
                inline=False,
            )
        if suggested and isinstance(suggested, dict) and len(suggested) > 0:
            adj_lines = [f"• **{k}**: {v}" for k, v in list(suggested.items())[:8]]
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
        await _channel.send(message)
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
    """Alert during an active trade (temperature drop, hard block breach, etc.)."""
    if _channel is None:
        return
    try:
        if alert_type == "temperature_drop":
            title = "🌡️ Temperatuur zakte tijdens trade"
            color = 0xF97316  # orange
        elif alert_type == "hard_block_breach":
            title = "🚨 KRITIEKE temperatuurdaling — overweeg abort"
            color = 0xEF4444  # red
        else:
            title = f"⚡ {alert_type}"
            color = 0x6B7280  # grey

        embed = discord.Embed(
            title=f"{title} — {_coin_label(coin)}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Coin", value=_coin_label(coin), inline=True)
        embed.add_field(name="Side", value=side.upper(), inline=True)
        embed.add_field(name="Mid prijs", value=f"{cur_mid:.4f}", inline=True)
        embed.add_field(name="Seconden resterend", value=f"{int(secs_rem)}s", inline=True)
        embed.set_footer(text=f"Trade ID: {trade_id[:8]}")
        await _channel.send(embed=embed)
    except Exception as exc:
        log.warning("discord_send_mid_trade_alert_failed", error=str(exc))


# ── Slash commands ────────────────────────────────────────────────────────────

def _register_slash_commands(bot: commands.Bot) -> None:
    """Register all slash commands on the bot tree."""

    @bot.tree.command(name="status", description="Toon de huidige bot-status en P&L")
    async def cmd_status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .state import get_mode, get_active_trades as _get_active
            from .db_sync import get_daily_pnl, get_state

            mode = get_mode()
            daily_pnl = get_daily_pnl()
            open_count = len(_get_active())

            track_record_raw = get_state("oracle_track_record") or "–"
            fg_raw = get_state("oracle_fear_greed_value") or "–"
            fg_label = get_state("oracle_fear_greed_label") or ""
            temp_raw = get_state("oracle_trading_temperature")
            temperature = int(float(temp_raw)) if temp_raw else 0

            embed = discord.Embed(
                title="🤖 Poly-Baws-Bot Status",
                color=_temp_color(temperature),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Mode", value=mode, inline=True)
            embed.add_field(name="Open trades", value=str(open_count), inline=True)
            embed.add_field(
                name="Vandaag P&L",
                value=f"€{_pnl_sign(daily_pnl)} USDC",
                inline=True,
            )
            embed.add_field(
                name="Temperatuur",
                value=f"{_temp_display(temperature)} ({temperature})",
                inline=True,
            )
            embed.add_field(
                name="Fear & Greed",
                value=f"{fg_raw} ({fg_label})" if fg_label else str(fg_raw),
                inline=True,
            )
            embed.add_field(
                name="Oracle Track Record",
                value=str(track_record_raw),
                inline=True,
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_status_failed", error=str(exc))
            await interaction.followup.send(
                f"❌ Fout bij ophalen status: {exc}", ephemeral=True
            )

    @bot.tree.command(name="pause", description="Schakel naar paper modus (pauzeer live trading)")
    async def cmd_pause(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .state import get_mode, set_mode

            prev = get_mode()
            if prev.startswith("paper"):
                await interaction.followup.send(
                    "ℹ️ Bot staat al in paper modus.", ephemeral=True
                )
                return
            set_mode("paper_auto")
            log.info("discord_pause_command", prev_mode=prev, new_mode="paper_auto")
            embed = discord.Embed(
                title="⏸️ Bot gepauzeerd",
                description=(
                    f"Modus gewisseld van **{prev}** → **paper_auto**\n"
                    "Live trading is gestopt."
                ),
                color=0xEAB308,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_pause_failed", error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(
        name="resume",
        description="Zet bot terug naar live modus (vraagt bevestiging)",
    )
    async def cmd_resume(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .state import get_mode, set_mode

            current = get_mode()
            if not current.startswith("paper"):
                await interaction.followup.send(
                    "ℹ️ Bot staat al in live modus.", ephemeral=True
                )
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
                log.info(
                    "discord_resume_command",
                    prev_mode=current,
                    new_mode=new_mode,
                )
            else:
                log.info("discord_resume_cancelled")
        except Exception as exc:
            log.warning("discord_cmd_resume_failed", error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(
        name="stop_coin",
        description="Schakel een specifieke coin uit via coin_guard",
    )
    @app_commands.describe(coin="De coin om uit te schakelen (bijv. BTC)")
    async def cmd_stop_coin(interaction: discord.Interaction, coin: str) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .coin_guard import _do_disable

            coin_upper = coin.upper()
            asyncio.create_task(
                _do_disable(
                    coin_upper,
                    f"discord_command — {interaction.user}",
                )
            )
            log.info("discord_stop_coin", coin=coin_upper, user=str(interaction.user))
            embed = discord.Embed(
                title=f"🚫 Coin uitgeschakeld: {_coin_label(coin_upper)}",
                description=f"**{coin_upper}** wordt uitgeschakeld via coin_guard.",
                color=0xEF4444,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_stop_coin_failed", coin=coin, error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(
        name="enable_coin",
        description="Heractiveer een uitgeschakelde coin",
    )
    @app_commands.describe(coin="De coin om te heractiveren (bijv. BTC)")
    async def cmd_enable_coin(interaction: discord.Interaction, coin: str) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .coin_guard import enable_coin

            coin_upper = coin.upper()
            asyncio.create_task(enable_coin(coin_upper))
            log.info("discord_enable_coin", coin=coin_upper, user=str(interaction.user))
            embed = discord.Embed(
                title=f"✅ Coin heractiveerd: {_coin_label(coin_upper)}",
                description=f"**{coin_upper}** wordt weer ingeschakeld.",
                color=0x22C55E,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_enable_coin_failed", coin=coin, error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(
        name="analyse",
        description="Start onmiddellijk een Oracle dagelijkse analyse",
    )
    async def cmd_analyse(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .oracle import run_daily_analysis

            await interaction.followup.send(
                "⏳ Oracle analyse wordt gestart...", ephemeral=False
            )
            asyncio.create_task(run_daily_analysis())
            log.info("discord_cmd_analyse", user=str(interaction.user))
        except Exception as exc:
            log.warning("discord_cmd_analyse_failed", error=str(exc))
            await interaction.followup.send(
                f"❌ Fout bij analyse: {exc}", ephemeral=True
            )

    @bot.tree.command(
        name="oracle",
        description="Zet de Oracle hard_gate aan of uit",
    )
    @app_commands.describe(on_off="'aan' om te activeren, 'uit' om te deactiveren")
    @app_commands.choices(
        on_off=[
            app_commands.Choice(name="aan", value="aan"),
            app_commands.Choice(name="uit", value="uit"),
        ]
    )
    async def cmd_oracle(interaction: discord.Interaction, on_off: str) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            oracle_cfg = CONFIG.setdefault("oracle", {})
            enabled = on_off == "aan"
            oracle_cfg["hard_gate"] = enabled
            status_word = "geactiveerd ✅" if enabled else "gedeactiveerd 🚫"
            log.info(
                "discord_oracle_toggle",
                hard_gate=enabled,
                user=str(interaction.user),
            )
            embed = discord.Embed(
                title=f"Oracle hard_gate {status_word}",
                description=f"Oracle hard_gate is nu **{'aan' if enabled else 'uit'}**.",
                color=0x22C55E if enabled else 0xEF4444,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning("discord_cmd_oracle_failed", error=str(exc))
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)

    @bot.tree.command(
        name="abort_trade",
        description="Force-abort een actieve trade",
    )
    @app_commands.describe(trade_id="Het trade ID om te annuleren")
    async def cmd_abort_trade(
        interaction: discord.Interaction, trade_id: str
    ) -> None:
        await interaction.response.defer(ephemeral=False)
        try:
            from .logger import update_trade as _db_update_trade, get_active_trades as _db_get_active
            from .state import remove_active_trade, get_active_trades

            active = get_active_trades()
            if trade_id not in active:
                await interaction.followup.send(
                    f"⚠️ Trade **{trade_id}** niet gevonden in actieve trades.",
                    ephemeral=True,
                )
                return

            trade = active[trade_id]
            coin = trade.get("coin", "–")

            # Mark status in DB
            await _db_update_trade(trade_id, {"status": "aborted"})
            # Remove from in-memory registry
            remove_active_trade(trade_id)

            log.info(
                "discord_abort_trade",
                trade_id=trade_id,
                coin=coin,
                user=str(interaction.user),
            )
            embed = discord.Embed(
                title=f"🛑 Trade afgebroken — {_coin_label(coin)}",
                description=(
                    f"Trade `{trade_id}` is gemarkeerd als **aborted** "
                    "en verwijderd uit actieve trades."
                ),
                color=0xEF4444,
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        except Exception as exc:
            log.warning(
                "discord_cmd_abort_trade_failed",
                trade_id=trade_id,
                error=str(exc),
            )
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)
