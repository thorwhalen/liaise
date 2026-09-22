"""Tests for the Claude Code hook (liaise #37, slice L4): ``liaise vet --hook`` and ``liaise hook``.

Every channel is a FakeGitHubChannel, every disclosure the invented one of the outbound
suite (Ada, Bram, Heron), every settings file and config root is under tmp_path, and the
checkout's repository is given, never read: nothing is sent and nothing real is read or
written. The hook never answers ``allow``: a write the gate would send, and every command
that writes nothing, gets no answer, so the operator's own permission rules decide.
"""

from __future__ import annotations

import functools
import io
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from liaise import cli, hook, vet as vetting
from liaise.gate import outbound_policy
from liaise.ledger import Ledger
from liaise.testing import FakeGitHubChannel, demo_registry
from liaise.tests.outbound import fixtures

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
HERON = "The export fix is in. Heron slips to October."
EXPORT = "The export fix is in."


@pytest.fixture(autouse=True)
def no_real_acquaint(monkeypatch):
    monkeypatch.setitem(sys.modules, "acquaint", None)


def fixture_disclosure(people, *, projects=(), audience=None, today=None):
    return fixtures.disclosure_for(people, audience=audience)


@pytest.fixture
def registry():
    github = FakeGitHubChannel(clock=lambda: NOW, visibility="public")
    github.set_visibility("example/solo", "private", owner_type="User")
    return demo_registry(github=github)


@pytest.fixture
def root(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.toml").write_text(f'owner_login = "owner"\nstate_dir = "{(tmp_path / "state").as_posix()}"\n')
    return str(config)


class Hook:
    """``liaise vet --hook`` with a fake GitHub, the fixture disclosure and a ledger in memory."""

    def __init__(self, registry, root, monkeypatch, tmp_path):
        self.registry, self.root, self.monkeypatch, self.tmp_path = registry, root, monkeypatch, tmp_path
        self.store: dict = {}

    def run(self, payload, **kwargs) -> dict | None:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        shown = cli.vet(
            hook=True,
            root=self.root,
            registry=self.registry,
            now=NOW,
            disclosure=fixture_disclosure,
            store=self.store,
            repo_of=lambda cwd: "example/app",
            **kwargs,
        )
        return None if shown is None else json.loads(shown)

    def bash(self, command, **extra):
        payload = {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(self.tmp_path),
            **extra,
        }
        return self.run(payload)


@pytest.fixture
def run_hook(registry, root, monkeypatch, tmp_path):
    return Hook(registry, root, monkeypatch, tmp_path)


def decision(answer):
    return None if answer is None else answer["hookSpecificOutput"]["permissionDecision"]


def reason(answer):
    return answer["hookSpecificOutput"]["permissionDecisionReason"]


# ---- the acceptance line ----


def test_a_comment_naming_heron_on_a_public_issue_is_held_with_the_heron_reason(run_hook):
    # The hook's provenance is always unknown, and the taint rule (liaise #36) refuses
    # private content to a public audience outright, so this is deny, not only ask.
    for command in (
        f'gh issue comment 12 -R example/app --body "{HERON}"',
        f'gh issue comment example/app 12 --body "{HERON}"',
        f'gh issue comment https://github.com/example/app/issues/12 --body "{HERON}"',
    ):
        answer = run_hook.bash(command)
        assert decision(answer) == "deny", command
        assert "'project:heron' is amber" in reason(answer)
        assert "github:example/app#12" in reason(answer) and "world-readable" in reason(answer)


def test_a_plain_comment_is_ask_since_the_hooks_provenance_is_unknown(run_hook):
    answer = run_hook.bash(f'gh issue comment 12 --body "{EXPORT}"')
    assert decision(answer) == "ask"
    assert "provenance is unknown" in reason(answer)


def test_a_token_is_deny_and_never_quoted(run_hook):
    answer = run_hook.bash(f"gh issue comment 12 -R example/app --body 'key {fixtures.TOKEN}'")
    assert decision(answer) == "deny"
    assert fixtures.TOKEN not in json.dumps(answer)


def test_a_body_file_that_is_not_there_is_ask(run_hook):
    answer = run_hook.bash("gh issue comment 12 -R example/app --body-file missing.md")
    assert decision(answer) == "ask" and "could not read the body" in reason(answer)


def test_a_body_file_is_read_from_the_sessions_directory(run_hook, tmp_path):
    (tmp_path / "reply.md").write_text(HERON)
    answer = run_hook.bash("gh issue comment 12 -R example/app -F reply.md")
    assert decision(answer) == "deny" and "project:heron" in reason(answer)


@pytest.mark.parametrize(
    "command",
    [
        "gh issue list",
        "gh pr view 3 --json title",
        "gh api repos/example/app/issues",
        "git commit -m 'Heron'",
        "ls -la",
        "echo gh",
    ],
)
def test_a_command_that_writes_nothing_gets_no_answer(run_hook, command):
    assert run_hook.bash(command) is None


def test_a_send_the_gate_passes_gets_no_answer_never_allow(run_hook):
    # A user's private repository: named readers, retractable enough; still tainted, so
    # it is ask unless the policy passes it. The hook prints allow for nothing.
    answer = run_hook.bash(f'gh issue comment 3 -R example/solo --body "{EXPORT}"')
    assert decision(answer) in ("ask", None)
    assert "allow" not in json.dumps(answer or {})


def test_a_heredoc_body_on_stdin_is_read(run_hook):
    command = f"gh issue comment 12 -R example/app --body-file - <<'EOF'\n{HERON}\nEOF"
    answer = run_hook.bash(command)
    assert decision(answer) == "deny" and "project:heron" in reason(answer)
    piped = f"cat <<'EOF' | gh issue comment 12 -R example/app -F -\n{HERON}\nEOF"
    assert decision(run_hook.bash(piped)) == "deny"


def test_a_body_the_shell_computes_is_ask(run_hook):
    for command in (
        'gh issue comment 12 --body "$(cat reply.md)"',
        'gh issue comment 12 --body "$DRAFT"',
        "gh issue comment 12 --body-file - <<EOF\nHello $NAME\nEOF",
        "gh issue comment 12 --body-file -",
    ):
        answer = run_hook.bash(command)
        assert decision(answer) == "ask", command
        assert "could not read the body" in reason(answer) or "command substitution" in reason(answer)


def test_every_write_of_a_compound_command_is_judged_and_the_strictest_wins(run_hook):
    command = f"cd /work && gh issue list && gh issue comment 12 --body '{EXPORT}' ; gh pr comment 3 --body 'key {fixtures.TOKEN}'"
    answer = run_hook.bash(command)
    assert decision(answer) == "deny"
    assert "gh issue comment" in reason(answer) and "gh pr comment" in reason(answer)


@pytest.mark.parametrize(
    "command",
    [
        "eval \"gh issue comment 12 --body hi\"",
        "bash -c 'gh issue comment 12 --body hi'",
        "echo 12 | xargs gh issue comment --body hi",
        "x=$(gh issue comment 12 --body hi)",
        "gh issue comment 12 --body 'unterminated",
    ],
)
def test_a_write_the_hook_cannot_see_through_is_ask(run_hook, command):
    assert decision(run_hook.bash(command)) == "ask"


def test_gh_commands_that_carry_text_and_are_not_in_the_table_are_ask(run_hook):
    answer = run_hook.bash("gh release create v1 --notes 'Heron ships'")
    assert decision(answer) == "ask" and "not a command the hook knows" in reason(answer)


def test_gh_api_writes_are_vetted(run_hook, tmp_path):
    answer = run_hook.bash(f"gh api repos/example/app/issues/12/comments -f body='{HERON}'")
    assert decision(answer) == "deny" and "github:example/app#12" in reason(answer)
    (tmp_path / "body.json").write_text(json.dumps({"body": f"key {fixtures.TOKEN}"}))
    answer = run_hook.bash("gh api -X POST repos/example/app/issues --input body.json")
    assert decision(answer) == "deny"
    assert decision(run_hook.bash("gh api -X PATCH repos/example/app/issues/comments/9 -F body=@missing.md")) == "ask"
    assert decision(run_hook.bash("gh api -X POST user/repos -f name=x")) == "ask"


def test_gh_api_graphql_mutations_are_vetted_and_queries_are_not(run_hook):
    mutation = f"gh api graphql -f query='mutation($b: String!) {{ addComment(input: {{subjectId: \"X\", body: $b}}) {{ clientMutationId }} }}' -f b='{HERON}'"
    answer = run_hook.bash(mutation)
    assert decision(answer) == "deny" and "project:heron" in reason(answer)
    assert run_hook.bash("gh api graphql -f query='{ viewer { login } }'") is None


def test_correspond_writes_are_vetted(run_hook):
    answer = run_hook.bash(f"correspond send github:example/app#12 '{HERON}'")
    assert decision(answer) == "deny"
    assert run_hook.bash(f"correspond send github:example/app#12 '{HERON}' --dry-run") is None
    assert run_hook.bash("correspond read github:example/app#12") is None


def test_a_correspond_mcp_send_is_vetted(run_hook):
    payload = {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__correspond__send",
        "tool_input": {"ref": "github:example/app#12", "text": HERON},
    }
    answer = run_hook.run(payload)
    assert decision(answer) == "deny" and "project:heron" in reason(answer)
    payload["tool_input"] = {"ref": "github:example/app#12", "text": HERON, "dry_run": True}
    assert run_hook.run(payload) is None
    payload["tool_name"], payload["tool_input"] = "mcp__correspond__read", {"ref": "github:example/app#12"}
    assert run_hook.run(payload) is None


def test_input_the_hook_cannot_read_is_ask(run_hook):
    assert decision(run_hook.run("not json")) == "ask"
    assert decision(run_hook.run({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "gh"})) == "ask"


def test_vet_hook_refuses_flags_that_would_change_the_judgement(run_hook):
    answer = run_hook.run({"tool_name": "Bash", "tool_input": {"command": "ls"}}, untainted=True)
    assert decision(answer) == "ask" and "--untainted" in reason(answer)


def test_a_failure_while_vetting_is_ask(run_hook, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(vetting, "vet", broken)
    answer = run_hook.bash(f'gh issue comment 12 --body "{EXPORT}"')
    assert decision(answer) == "ask" and "boom" in reason(answer)


# ---- the override ----


def test_a_command_answered_ask_that_ran_is_recorded_as_an_override_without_its_text(run_hook):
    command = f'gh issue comment 12 -R example/app --body "{EXPORT}"'
    pre = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash",
           "tool_input": {"command": command}, "tool_use_id": "toolu_1", "cwd": "."}
    assert decision(run_hook.run(pre)) == "ask"
    ledger = Ledger(run_hook.store)
    assert [key for key, _ in ledger.hook_pending()] == ["toolu_1"]
    post = {**pre, "hook_event_name": "PostToolUse", "tool_response": {"stdout": "ok"}}
    assert run_hook.run(post) is None
    overrides = list(ledger.overrides())
    assert len(overrides) == 1
    override = overrides[0]
    assert override["kind"] == "override" and override["decision"] == "ask"
    assert override["writes"][0]["ref"] == "github:example/app#12"
    assert "taint" in override["writes"][0]["rules"]
    assert EXPORT not in json.dumps(run_hook.store)
    assert list(ledger.hook_pending()) == []
    run_hook.run(post)  # a second PostToolUse records nothing more
    assert len(list(ledger.overrides())) == 1


def test_a_post_tool_use_the_hook_did_not_ask_about_records_nothing(run_hook):
    post = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "gh issue list"}, "tool_use_id": "toolu_2"}
    assert run_hook.run(post) is None
    assert list(Ledger(run_hook.store).overrides()) == []


def test_without_a_tool_use_id_the_call_itself_is_the_key():
    one = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "gh issue comment 1 -b x"}}
    assert hook.pending_key(one) == hook.pending_key(dict(one)) != hook.pending_key({**one, "session_id": "t"})


def test_stale_pending_asks_are_pruned():
    ledger = Ledger({})
    old = (NOW - timedelta(hours=48)).isoformat()
    ledger.set_hook_pending("old", {"at": old})
    answer = hook.Answer(hook.ASK, ("r",))
    hook._prune(ledger, now=NOW)
    assert list(ledger.hook_pending()) == []
    assert answer.reason == "liaise: r"


# ---- installing ----


def test_install_writes_both_hooks_keeps_the_rest_and_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    other = {"matcher": "Write", "hooks": [{"type": "command", "command": "my-linter"}]}
    settings.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [other]}}))
    assert "installed" in cli.hook_install(settings=str(settings))
    first = settings.read_text()
    assert "already installed" in cli.hook_install(settings=str(settings))
    assert settings.read_text() == first
    data = json.loads(first)
    assert data["model"] == "x"
    assert data["hooks"]["PreToolUse"][0] == other
    for event in ("PreToolUse", "PostToolUse"):
        mine = [e for e in data["hooks"][event] if e.get("matcher") == hook.HOOK_MATCHER]
        assert len(mine) == 1 and mine[0]["hooks"][0]["command"] == "liaise vet --hook"
    assert cli.hook_status(settings=str(settings)) == "PreToolUse: installed\nPostToolUse: installed"


def test_status_and_uninstall(tmp_path):
    settings = tmp_path / "settings.json"
    assert cli.hook_status(settings=str(settings)) == "PreToolUse: missing\nPostToolUse: missing"
    cli.hook_install(settings=str(settings))
    data = json.loads(settings.read_text())
    data["hooks"]["PostToolUse"][0]["matcher"] = "Bash"
    settings.write_text(json.dumps(data))
    assert "PostToolUse: outdated" in cli.hook_status(settings=str(settings))
    assert "removed" in cli.hook_uninstall(settings=str(settings))
    assert json.loads(settings.read_text()) == {}
    assert "no liaise hooks" in cli.hook_uninstall(settings=str(settings))


def test_install_refuses_a_settings_file_it_cannot_read(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("[1, 2]")
    with pytest.raises(Exception) as raised:
        cli.hook_install(settings=str(settings))
    assert getattr(raised.value, "code", None) == 1
    assert settings.read_text() == "[1, 2]"


def test_the_matcher_takes_bash_and_correspond_write_tools_only():
    import re

    matcher = re.compile(hook.HOOK_MATCHER)
    for name in ("Bash", "mcp__correspond__send", "mcp__plugin_correspond__edit"):
        assert matcher.fullmatch(name), name
    for name in ("Write", "mcp__correspond__read", "mcp__other__send"):
        assert not matcher.fullmatch(name), name
