"""Persistent Discord View for elevation Approve/Deny buttons.

Each row in `elevation_requests` gets its own ElevationView bound to its
embed message via `bot.add_view(view, message_id=...)`. The view persists
across bot restarts: at boot we walk recent rows and re-bind, so a click on
an embed posted yesterday still resolves to a real handler instead of
Discord's generic "interaction failed".

Authorization: only `OPERATOR_USER_ID` may resolve a request. Anyone else
gets an ephemeral rejection — the row stays `pending` so the operator can
still act on it.

CAS: `mark_approved` / `mark_denied` use `WHERE uuid=? AND status='pending'`,
so a double-click resolves to one winner. The loser's interaction shows
"already resolved as <state>" and edits the embed if needed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord

from src.config import OPERATOR_USER_ID
from src.elevation_store import ElevationStore

log = logging.getLogger(__name__)


def _hhmmss() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _resolution_embed(
    base: discord.Embed,
    *,
    color: discord.Color,
    headline: str,
    actor: str,
) -> discord.Embed:
    """Return a copy of the original embed with a resolution banner appended.

    Keeps the original fields (reason, command, cwd, timeouts, warning) so the
    audit trail in Discord stays readable. Prepends the headline + actor +
    timestamp to the description.
    """
    new = discord.Embed(
        title=base.title,
        color=color,
        description=f"**{headline}** — {actor} at {_hhmmss()}\n\n{base.description or ''}".rstrip(),
    )
    for field in base.fields:
        new.add_field(name=field.name, value=field.value, inline=field.inline)
    if base.footer and base.footer.text:
        new.set_footer(text=base.footer.text)
    return new


class _ApproveButton(discord.ui.Button["ElevationView"]):
    def __init__(self, uuid: str):
        super().__init__(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id=f"elev:approve:{uuid}",
        )
        self.uuid = uuid

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._handle_click(interaction, self.uuid, approve=True)


class _DenyButton(discord.ui.Button["ElevationView"]):
    def __init__(self, uuid: str):
        super().__init__(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id=f"elev:deny:{uuid}",
        )
        self.uuid = uuid

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._handle_click(interaction, self.uuid, approve=False)


class ElevationView(discord.ui.View):
    def __init__(self, uuid: str, store: ElevationStore):
        super().__init__(timeout=None)
        self.uuid = uuid
        self.store = store
        self.add_item(_ApproveButton(uuid))
        self.add_item(_DenyButton(uuid))

    async def _handle_click(
        self,
        interaction: discord.Interaction,
        uuid: str,
        *,
        approve: bool,
    ) -> None:
        if OPERATOR_USER_ID is None or interaction.user.id != OPERATOR_USER_ID:
            await interaction.response.send_message(
                "Not authorized — only the operator can resolve elevation requests.",
                ephemeral=True,
            )
            return

        row = self.store.get(uuid)
        if row is None:
            await interaction.response.send_message(
                f"Request {uuid} not found (deleted from store).",
                ephemeral=True,
            )
            return

        if row["status"] != "pending":
            await interaction.response.send_message(
                f"Already resolved as **{row['status']}** — no action taken.",
                ephemeral=True,
            )
            return

        actor = f"<@{interaction.user.id}>"
        if approve:
            won = self.store.mark_approved(uuid, approved_by=str(interaction.user.id))
            color = discord.Color.green()
            headline = "✓ Approved"
        else:
            won = self.store.mark_denied(uuid, approved_by=str(interaction.user.id))
            color = discord.Color.red()
            headline = "✗ Denied"

        if not won:
            # Lost the CAS — another click won the race in the microseconds
            # between our get() and our update(). Refresh and report.
            fresh = self.store.get(uuid)
            await interaction.response.send_message(
                f"Already resolved as **{fresh['status'] if fresh else 'unknown'}** "
                "(double-click race) — no action taken.",
                ephemeral=True,
            )
            return

        # Wake the awaiting tool call. Done before the embed edit so the tool
        # races ahead while we update Discord.
        self.store.event_for(uuid).set()

        # Disable buttons + replace embed with resolution.
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        original = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(title="Elevation request")
        await interaction.response.edit_message(
            embed=_resolution_embed(original, color=color, headline=headline, actor=actor),
            view=self,
        )
