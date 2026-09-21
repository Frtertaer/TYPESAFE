from __future__ import annotations

from typer.core import TyperCommand, TyperGroup

from typesafe_cli.detect import is_agent_mode
from typesafe_cli.format import dumps, success_envelope
from typesafe_cli.schema import schema_for_help


def _help_is_agent(ctx) -> bool:  # noqa: ANN001 — click Context
    if ctx.params.get("no_agent"):
        return False
    if ctx.params.get("agent"):
        return True
    parent = ctx.parent
    while parent is not None:
        if parent.params.get("no_agent"):
            return False
        if parent.params.get("agent"):
            return True
        parent = parent.parent
    return is_agent_mode()


class AgentGroup(TyperGroup):
    def get_help(self, ctx) -> str:  # noqa: ANN001
        if _help_is_agent(ctx):
            return dumps(success_envelope(data=schema_for_help(ctx), command="help")).rstrip("\n")
        return super().get_help(ctx)


class AgentCommand(TyperCommand):
    def get_help(self, ctx) -> str:  # noqa: ANN001
        if _help_is_agent(ctx):
            return dumps(success_envelope(data=schema_for_help(ctx), command="help")).rstrip("\n")
        return super().get_help(ctx)
