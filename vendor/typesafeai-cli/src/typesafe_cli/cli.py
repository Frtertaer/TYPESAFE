from __future__ import annotations

import typer

from typesafe_cli.commands.agent import schema as agent_schema
from typesafe_cli.commands.ask import ask
from typesafe_cli.commands.auth import status as auth_status
from typesafe_cli.commands.decide import decide
from typesafe_cli.commands.extract import extract
from typesafe_cli.commands.find import find
from typesafe_cli.commands.models import models
from typesafe_cli.commands.oneshot import choice, noul, score
from typesafe_cli.commands.rank import rank
from typesafe_cli.commands.screen import screen
from typesafe_cli.commands.skills import install as skills_install
from typesafe_cli.commands.skills import list_skills
from typesafe_cli.commands.smoke import smoke
from typesafe_cli.commands.suggest_skill import suggest_skill
from typesafe_cli.commands.verify import verify
from typesafe_cli.help import AgentCommand, AgentGroup

app = typer.Typer(
    name="typesafe",
    cls=AgentGroup,
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Agent CLI for TypeSafe System One (Jev). Typed judgments, not chat. Invalid answers fail closed. Agents must not read TYPESAFE_* env vars or key files; use typesafe auth status.",
)


def _on_agent(args: bool = False) -> bool:
    if args:
        from typesafe_cli.detect import set_force_agent

        set_force_agent(True)
    return args


def _on_no_agent(args: bool = False) -> bool:
    if args:
        from typesafe_cli.detect import set_force_agent

        set_force_agent(False)
    return args


@app.callback()
def _global_options(
    agent: bool = typer.Option(
        False,
        "--agent",
        help="Force agent mode (JSON help/schema)",
        is_eager=True,
        callback=_on_agent,
    ),
    no_agent: bool = typer.Option(
        False,
        "--no-agent",
        help="Disable agent mode",
        is_eager=True,
        callback=_on_no_agent,
    ),
) -> None:
    del agent, no_agent


auth_app = typer.Typer(cls=AgentGroup, no_args_is_help=True, help="Authentication helpers")
auth_app.command("status", cls=AgentCommand)(auth_status)
app.add_typer(auth_app, name="auth")

agent_app = typer.Typer(cls=AgentGroup, no_args_is_help=True, help="Agent discovery")
agent_app.command("schema", cls=AgentCommand)(agent_schema)
app.add_typer(agent_app, name="agent")

skills_app = typer.Typer(cls=AgentGroup, no_args_is_help=True, help="Install the official TypeSafe skill")
skills_app.command("install", cls=AgentCommand)(skills_install)
skills_app.command("list", cls=AgentCommand)(list_skills)
app.add_typer(skills_app, name="skills")

app.command("ask", cls=AgentCommand)(ask)
app.command("noul", cls=AgentCommand)(noul)
app.command("choice", cls=AgentCommand)(choice)
app.command("score", cls=AgentCommand)(score)
app.command("find", cls=AgentCommand)(find)
app.command("rank", cls=AgentCommand)(rank)
app.command("extract", cls=AgentCommand)(extract)
app.command("verify", cls=AgentCommand)(verify)
app.command("screen", cls=AgentCommand)(screen)
app.command("suggest-skill", cls=AgentCommand)(suggest_skill)
app.command("decide", cls=AgentCommand)(decide)
app.command("models", cls=AgentCommand)(models)
app.command("smoke", cls=AgentCommand)(smoke)
