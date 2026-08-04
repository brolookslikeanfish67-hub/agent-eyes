# -*- coding: utf-8 -*-
"""Environment health checker — powered by channels.

Each channel knows how to check itself. Doctor just collects the results.
"""

from __future__ import annotations

import stat
import sys
from typing import TYPE_CHECKING

from rich.markup import escape as rich_escape

from agent_reach.channels import get_all_channels
from agent_reach.config import Config
from agent_reach.utils.text import scrub_url_credentials

if TYPE_CHECKING:
    from typing import TypedDict

    class ChannelResult(TypedDict):
        status: str
        name: str
        message: str
        tier: int
        backends: list[str]
        active_backend: str | None


# ── Constants ────────────────────────────────────────────────────────────────

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_OFF = "off"
STATUS_ERROR = "error"

TIER_ZERO = 0
TIER_ONE = 1
TIER_TWO = 2

ICON_OK = "✅"
ICON_WARN = "⚠"
ICON_ERROR = "❌"

STATUS_COLOR = {
    STATUS_OK: "green",
    STATUS_WARN: "yellow",
    STATUS_OFF: "red",
    STATUS_ERROR: "red",
}


# ── Public API ───────────────────────────────────────────────────────────────

def check_all(config: Config) -> dict[str, ChannelResult]:
    """Check every channel and return a status dictionary.

    A single misbehaving channel must never take the whole report down,
    so per-channel exceptions degrade to ``status="error"``.
    """
    results: dict[str, ChannelResult] = {}

    for channel in get_all_channels():
        try:
            status, message = channel.check(config)
            active_backend = getattr(channel, "active_backend", None)
        except Exception as exc:  # noqa: BLE001 — doctor must survive any channel
            status = STATUS_ERROR
            message = f"体检异常：{exc}"
            active_backend = None

        message = scrub_url_credentials(message)

        results[channel.name] = {
            "status": status,
            "name": channel.description,
            "message": message,
            "tier": channel.tier,
            "backends": channel.backends,
            "active_backend": active_backend,
        }

    return results


def format_report(results: dict[str, ChannelResult]) -> str:
    """Format results as a readable Rich-markup text report."""
    lines: list[str] = []

    _write_header(lines)
    _write_tiers(lines, results)
    _write_summary(lines, results)
    _write_security_note(lines)

    return "\n".join(lines)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _write_header(lines: list[str]) -> None:
    lines.append("[bold cyan]Agent Reach 状态[/bold cyan]")
    lines.append("[cyan]" + "=" * 40 + "[/cyan]")
    lines.append(
        "图例：[green]" + ICON_OK + "[/green] 可用  "
        "[yellow]" + ICON_WARN + "[/yellow] 已装但需配置/登录  "
        "[red]" + ICON_ERROR + "[/red] 未安装"
    )


def _render_line(result: ChannelResult) -> str:
    """Render one channel line; show the active backend when there is a choice."""
    text = f"[bold]{rich_escape(result['name'])}[/bold] — {rich_escape(result['message'])}"
    active = result.get("active_backend")
    backends = result.get("backends", [])
    if active and len(backends) > 1:
        text += f" [dim]（当前后端：{rich_escape(active)}）[/dim]"
    return text


def _write_tiers(lines: list[str], results: dict[str, ChannelResult]) -> None:
    """Group and render channels by tier."""
    by_tier: dict[int, list[ChannelResult]] = {TIER_ZERO: [], TIER_ONE: [], TIER_TWO: []}
    for r in results.values():
        by_tier.setdefault(r["tier"], []).append(r)

    # Tier 0 — zero config
    _write_tier_section(lines, by_tier[TIER_ZERO], "✅ 装好即用：")

    # Tier 1 — needs free key / login
    _write_tier_section(lines, by_tier[TIER_ONE], "可选渠道（已安装）：")

    # Tier 2 — optional complex setup
    _write_tier_section(lines, by_tier[TIER_TWO], "可选渠道（已安装）：")


def _write_tier_section(
    lines: list[str],
    items: list[ChannelResult],
    heading: str,
) -> None:
    """Render one tier section; only emit heading when there are active items."""
    active = [r for r in items if r["status"] == STATUS_OK]
    if not active:
        return

    lines.append("")
    lines.append(f"[bold]{heading}[/bold]")
    for r in active:
        icon = STATUS_COLOR.get(r["status"], "red")
        lines.append(f"  [{icon}]{ICON_OK}[/{icon}] {_render_line(r)}")


def _write_summary(lines: list[str], results: dict[str, ChannelResult]) -> None:
    """Write the overall status count and inactive-channel hint."""
    ok_count = sum(1 for r in results.values() if r["status"] == STATUS_OK)
    total = len(results)

    lines.append("")
    color = "green" if ok_count == total else ("yellow" if ok_count > 0 else "red")
    lines.append(f"状态：[{color}]{ok_count}/{total}[/{color}] 个渠道可用")

    inactive = [r for r in results.values() if r["status"] != STATUS_OK and r["tier"] > TIER_ZERO]
    if inactive:
        names = [r["name"] for r in inactive]
        lines.append(
            f"还有 {len(names)} 个可选渠道可以解锁（{'、'.join(names)}），"
            "告诉你的 Agent「帮我装 XXX」即可"
        )


def _write_security_note(lines: list[str]) -> None:
    """Warn if config.yaml is world-readable (Unix only)."""
    if sys.platform == "win32":
        return

    config_path = Config.CONFIG_DIR / "config.yaml"
    if not config_path.exists():
        return

    try:
        mode = config_path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            lines.append("")
            lines.append(
                "[bold red][!]  安全提示：config.yaml 权限过宽（其他用户可读）[/bold red]"
            )
            lines.append("   修复：chmod 600 ~/.agent-reach/config.yaml")
    except OSError:
        pass
