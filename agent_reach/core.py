# -*- coding: utf-8 -*-
"""
AgentReach — installer, doctor, and configuration tool.

Agent Reach helps AI agents install and configure upstream platform tools
(twitter-cli, yt-dlp, mcporter, gh CLI, etc.). After installation, agents
call the upstream tools directly — no wrapper layer needed.

Usage:
    from agent_reach import AgentReach

    reach = AgentReach()
    report = reach.doctor_report()
    print(report)
"""

import asyncio
from typing import Any, Dict, Optional, Tuple, Type


class AgentReach:
    """Enterprise manager for AI Agent environment readiness and health diagnostics.

    Provides sync and async interfaces to verify channel configurations and tool availability.
    """

    __slots__ = ("_config", "_doctor_module")

    def __init__(self, config: Optional[Any] = None) -> None:
        """Initialize AgentReach with optional custom configuration."""
        if config is None:
            from agent_reach.config import Config
            self._config = Config()
        else:
            self._config = config
            
        self._doctor_module: Optional[Any] = None

    @property
    def config(self) -> Any:
        """Access the current configuration object."""
        return self._config

    def _get_doctor(self) -> Any:
        """Lazy-load and cache the doctor module to reduce import overhead."""
        if self._doctor_module is None:
            import agent_reach.doctor as doctor_mod
            self._doctor_module = doctor_mod
        return self._doctor_module

    def doctor(self) -> Dict[str, Dict[str, Any]]:
        """Perform a synchronous health check across all configured channels."""
        try:
            doctor = self._get_doctor()
            return doctor.check_all(self._config)
        except Exception as err:
            return {
                "system": {
                    "status": "error",
                    "message": f"Failed to execute health check: {str(err)}"
                }
            }

    def doctor_report(self) -> str:
        """Generate a formatted human/agent-readable health report."""
        try:
            doctor = self._get_doctor()
            results = doctor.check_all(self._config)
            return doctor.format_report(results)
        except Exception as err:
            return f"[AgentReach Doctor Failure]: {str(err)}"

    async def adoctor(self) -> Dict[str, Dict[str, Any]]:
        """Perform health checks asynchronously to avoid blocking event loops."""
        return await asyncio.to_thread(self.doctor)

    async def adoctor_report(self) -> str:
        """Generate a formatted health report asynchronously."""
        return await asyncio.to_thread(self.doctor_report)

    def fix_all(self) -> Dict[str, Any]:
        """Attempt automated remediation for failing dependencies if supported."""
        doctor = self._get_doctor()
        if hasattr(doctor, "fix_all"):
            return doctor.fix_all(self._config)
        raise NotImplementedError("Automated fixing is not supported by the underlying doctor module.")

    def __repr__(self) -> str:
        return f"<AgentReach config={self._config.__class__.__name__}>"
