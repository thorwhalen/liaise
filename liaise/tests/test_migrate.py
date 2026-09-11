"""Tests for liaise.migrate: 0.0.x partner files to 0.1 subject files, the plan, and the TOML writer.

Every config tree is built under ``tmp_path``. Partner files are written by
``_write_toml`` below, independently of the writer under test, and logins are built
from the partner slug, so no login literal outside the fictional allowlist appears here.
"""

from __future__ import annotations

import json
import socket
import subprocess
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from correspond.routing import binding_matches
from correspond.testing import demo_message

from liaise import migrate as migrate_module
from liaise.config import EscalateConfig, Markers
from liaise.migrate import InlineTable, dumps_toml, migrate_config
from liaise.subjects import (
    BudgetPolicy,
    Delivery,
    ProcessorConfig,
    ReadinessPolicy,
    Workspace,
    load_subject,
)

APP, SITE = "example/app", "example/site"
CHECKOUT = "~/code/example-app"
PAT_BINDING = "github:example/app?labels=partner:pat"


def _write_toml(path: Path, doc: dict) -> None:
    """Write ``doc`` (scalars, arrays, one level of tables) as TOML; JSON values are TOML values."""
    top = [f"{k} = {json.dumps(v)}" for k, v in doc.items() if not isinstance(v, dict)]
    tables = [
        f"\n[{key}]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in table.items())
        for key, table in doc.items()
        if isinstance(table, dict)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(top + tables) + "\n")


def _partner(slug: str, *, repo: str = APP, **fields) -> dict:
    """A 0.0.x partner file's content, its one login the slug."""
    return {
        "display_name": slug.title(),
        "github_logins": [slug],
        "repo": repo,
        "brief": f"~/.config/liaise/briefs/{slug}.md",
        "deploy": "./deploy.sh",
        "dispatch": {"cwd": CHECKOUT},
        **fields,
    }


def _full_pat() -> dict:
    """Pat with every mapped 0.0.x key set to a non-default value."""
    return _partner(
        "pat",
        label="partner:pat",
        reply_mode="direct",
        verify="npm test",
        label_prefix="helper:",
        deploy_per="issue",
        quiet_minutes=15,
        go_minutes=3,
        deployed_nudge_days=5,
        markers={"go": "#go#", "wait": "#hold#"},
        budget={"timeout_minutes": 30, "max_turns": 100, "daily_dispatches": 4},
        escalate={"money_usd": 25.0, "max_scope": "an afternoon"},
        dispatch={"cwd": CHECKOUT, "permission_mode": "acceptEdits"},
        **{"notify_login": "octocat"},
    )


def _config(tmp_path: Path, partners: dict[str, dict]) -> Path:
    """A fictional 0.0.x ``~/.config/liaise`` tree: config.toml, partner files, briefs."""
    root = tmp_path / "config"
    _write_toml(
        root / "config.toml",
        {"owner_login": "owner", "state_dir": (tmp_path / "state").as_posix()},
    )
    for slug, fields in partners.items():
        _write_toml(root / "partners" / f"{slug}.toml", fields)
        (root / "briefs").mkdir(exist_ok=True)
        (root / "briefs" / f"{slug}.md").write_text(f"{slug.title()} is fictional.\n")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _outside_subjects(snapshot: dict[str, bytes]) -> dict[str, bytes]:
    return {k: v for k, v in snapshot.items() if not k.startswith("subjects/")}


def _doc(plan, index: int = 0) -> dict:
    return tomllib.loads(plan.subjects[index].toml_text)


# ---- the field mapping ----


def test_one_partner_becomes_one_subject_with_every_mapped_field(tmp_path):
    root = _config(tmp_path, {"pat": _full_pat()})
    plan = migrate_config(root)

    (subject,) = plan.subjects
    assert subject.slug == "example-app"
    assert subject.path == root / "subjects" / "example-app.toml"
    assert subject.sources == (root / "config.toml", root / "partners" / "pat.toml")
    assert (subject.conflicts, subject.warnings, plan.warnings) == ((), (), ())
    assert tomllib.loads(subject.toml_text) == {
        "bindings": [PAT_BINDING],
        "workspace": {"kind": "shared", "path": CHECKOUT},
        "brief": "~/.config/liaise/briefs/pat.md",
        "verify": "npm test",
        "delivery": {"kind": "deploy", "per": "issue", "command": "./deploy.sh"},
        "label_prefix": "helper:",
        "policy": {
            "default_reply_mode": "direct",
            "people": {"github:pat": "pat"},
            "roles": {"pat": "partner"},
            "relays": ["github:owner"],
            "claim_labels": {"partner:pat": "pat"},
            "notify": {"pat": "github:octocat"},
            "briefs": {"pat": "~/.config/liaise/briefs/pat.md"},
            "deployed_nudge_days": 5,
            "readiness": {
                "quiet_minutes": 15,
                "go_minutes": 3,
                "markers": {"go": "#go#", "wait": "#hold#"},
            },
            "escalate": {"money_usd": 25.0, "max_scope": "an afternoon"},
            "budget": {
                "concurrent": 1,
                "timeout_minutes": 30,
                "max_turns": 100,
                "daily_dispatches": 4,
            },
        },
        "processor": {"permission_mode": "acceptEdits"},
    }


def test_values_0_0_x_only_defaulted_are_left_to_the_0_1_defaults(tmp_path):
    root = _config(tmp_path, {"pat": _partner("pat")})
    doc = _doc(migrate_config(root))
    assert set(doc) == {"bindings", "workspace", "brief", "delivery", "policy"}
    assert doc["delivery"] == {"kind": "deploy", "per": "batch", "command": "./deploy.sh"}
    assert doc["policy"]["default_reply_mode"] == "draft"
    assert doc["policy"]["notify"] == {"pat": "github:pat"}  # the first login
    assert doc["policy"]["budget"] == {"concurrent": 1}
    for unset in ("readiness", "escalate", "deployed_nudge_days", "reply_modes"):
        assert unset not in doc["policy"]


@pytest.mark.parametrize(
    "deploy_per, per, warned",
    [("batch", "batch", False), ("issue", "issue", False), ("case", "issue", True)],
)
def test_deploy_per_becomes_batch_or_issue_and_any_other_value_is_a_warning(tmp_path, deploy_per, per, warned):
    """S7 #4: a 0.1 subject takes only batch or issue, and 0.0.x deployed each issue on its
    own for any deploy_per but batch."""
    plan = migrate_config(_config(tmp_path, {"pat": _partner("pat", deploy_per=deploy_per)}), apply=True)
    (subject,) = plan.subjects
    assert _doc(plan)["delivery"]["per"] == per
    assert load_subject(plan.written[0]).delivery.per == per
    warnings = [*subject.warnings, *plan.warnings]
    assert any("deploy_per 'case'" in w and "delivery.per becomes issue" in w for w in warnings) is warned


def test_a_value_set_in_config_toml_is_carried(tmp_path):
    root = _config(tmp_path, {"pat": _partner("pat")})
    config_toml = root / "config.toml"
    config_toml.write_text("quiet_minutes = 20\n" + config_toml.read_text())
    assert _doc(migrate_config(root))["policy"]["readiness"] == {"quiet_minutes": 20}


def test_two_partners_sharing_a_repo_become_one_subject(tmp_path):
    root = _config(
        tmp_path,
        {
            "pat": _partner("pat", budget={"daily_dispatches": 8}),
            "sam": _partner("sam", reply_mode="direct", budget={"daily_dispatches": 3}),
        },
    )
    plan = migrate_config(root, apply=True)

    (subject,) = plan.subjects
    assert subject.sources == (
        root / "config.toml",
        root / "partners" / "pat.toml",
        root / "partners" / "sam.toml",
    )
    doc, policy = _doc(plan), _doc(plan)["policy"]
    assert doc["bindings"] == [PAT_BINDING, "github:example/app?labels=partner:sam"]
    assert policy["people"] == {"github:pat": "pat", "github:sam": "sam"}
    assert policy["roles"] == {"pat": "partner", "sam": "partner"}
    assert policy["claim_labels"] == {"partner:pat": "pat", "partner:sam": "sam"}
    assert policy["notify"] == {"pat": "github:pat", "sam": "github:sam"}
    assert (policy["default_reply_mode"], policy["reply_modes"]) == ("draft", {"sam": "direct"})
    assert policy["budget"] == {"concurrent": 1, "daily_dispatches": 3}

    budget_note = "policy.budget.daily_dispatches: kept the strictest, 3; pat has 8"
    assert budget_note in subject.conflicts
    assert f"  conflict: {budget_note}" in plan.lines()
    assert "brief" not in doc  # their briefs differ, so the subject has none of its own
    assert policy["briefs"] == {
        "pat": "~/.config/liaise/briefs/pat.md",
        "sam": "~/.config/liaise/briefs/sam.md",
    }
    assert not any("brief" in conflict for conflict in subject.conflicts)

    assert not any("does not load" in warning for warning in subject.warnings)
    loaded = load_subject(subject.path)
    assert loaded.reply_mode_for("sam") == "direct"
    assert loaded.reply_mode_for("pat") == "draft"
    assert (loaded.brief, loaded.brief_for("pat"), loaded.brief_for("sam")) == (
        "",
        "~/.config/liaise/briefs/pat.md",
        "~/.config/liaise/briefs/sam.md",
    )


def test_partners_sharing_one_brief_keep_it_as_the_subjects_brief(tmp_path):
    shared = "~/.config/liaise/briefs/example-app.md"
    root = _config(tmp_path, {"pat": _partner("pat", brief=shared), "sam": _partner("sam", brief=shared)})
    plan = migrate_config(root)
    doc = _doc(plan)
    assert doc["brief"] == shared
    assert doc["policy"]["briefs"] == {"pat": shared, "sam": shared}
    assert plan.subjects[0].conflicts == ()


def test_bindings_match_by_label_and_a_note_says_how_to_opt_in_to_author_matching(tmp_path):
    plan = migrate_config(_config(tmp_path, {"pat": _partner("pat")}))
    (subject,) = plan.subjects
    assert _doc(plan)["bindings"] == [PAT_BINDING]  # no author binding is written

    (note,) = subject.notes
    assert note == (
        'issues pat opens without the "partner:pat" label are not picked up (0.0.x matched '
        'them by author); to opt in, add "github:example/app?author=pat" to bindings'
    )
    assert f"  note: {note}" in plan.lines()
    suggested = note.split('add "')[1].split('"')[0]
    unlabelled = demo_message(conversation="github:example/app#12", handle="pat")
    assert binding_matches(suggested, unlabelled)
    assert not binding_matches(PAT_BINDING, unlabelled)


def test_two_partners_on_different_repos_become_two_subjects(tmp_path):
    root = _config(tmp_path, {"pat": _partner("pat"), "sam": _partner("sam", repo=SITE)})
    plan = migrate_config(root)
    assert [s.slug for s in plan.subjects] == ["example-app", "example-site"]
    assert _doc(plan, 0)["policy"]["people"] == {"github:pat": "pat"}
    assert _doc(plan, 1)["policy"]["people"] == {"github:sam": "sam"}
    assert _doc(plan, 1)["bindings"] == ["github:example/site?labels=partner:sam"]
    assert all(not s.conflicts for s in plan.subjects)


def test_repos_that_slugify_alike_each_add_their_owner(tmp_path):
    other = "/".join(("example-app", "x"))  # slugifies like example/app-x
    root = _config(
        tmp_path,
        {"pat": _partner("pat", repo="example/app-x"), "sam": _partner("sam", repo=other)},
    )
    plan = migrate_config(root)
    assert [s.slug for s in plan.subjects] == [
        "example-app-x--example",
        "example-app-x--example-app",
    ]
    assert any("'example-app-x'" in warning for warning in plan.warnings)


def test_a_label_with_glob_or_query_characters_binds_to_that_label_only(tmp_path):
    label = "needs review & *urgent* 100%"
    root = _config(tmp_path, {"pat": _partner("pat", label=label)})
    (binding,) = _doc(migrate_config(root))["bindings"]
    field, _, glob = binding.partition("?")[2].partition("=")
    assert field == "labels" and "&" not in glob
    assert fnmatchcase(label, unquote(glob))
    assert not fnmatchcase("needs review & xurgentx 100%", unquote(glob))


# ---- dry run and apply ----


def test_a_dry_run_writes_nothing(tmp_path):
    root = _config(tmp_path, {"pat": _partner("pat"), "sam": _partner("sam", repo=SITE)})
    before = _snapshot(root)

    plan = migrate_config(root)

    assert not (root / "subjects").exists()
    assert _snapshot(root) == before
    assert (plan.applied, plan.written) == (False, ())
    lines = plan.lines()
    assert lines[-1] == "nothing written (dry run)"
    for subject in plan.subjects:
        assert f"subject {subject.slug}: {subject.path}" in lines
        assert all(f"    {line}" in lines for line in subject.toml_text.splitlines() if line)
        assert any(str(root / "partners") in line for line in lines if line.startswith("  from:"))


def test_apply_writes_the_files_and_never_overwrites_one(tmp_path):
    root = _config(tmp_path, {"pat": _partner("pat"), "sam": _partner("sam", repo=SITE)})
    before = _snapshot(root)

    plan = migrate_config(root, apply=True)

    paths = [root / "subjects" / "example-app.toml", root / "subjects" / "example-site.toml"]
    assert list(plan.written) == paths
    assert [path.read_text() for path in paths] == [s.toml_text for s in plan.subjects]
    assert plan.lines()[-1] == "wrote 2 subject file(s)"
    assert _outside_subjects(_snapshot(root)) == before

    edited = "# edited by the operator\n" + paths[0].read_text()
    paths[0].write_text(edited)
    again = migrate_config(root, apply=True)

    assert again.written == ()
    assert again.lines()[-1] == "wrote 0 subject file(s)"
    assert paths[0].read_text() == edited
    for subject in again.subjects:
        assert any("already exists" in warning for warning in subject.warnings)
    assert _outside_subjects(_snapshot(root)) == before


def test_the_emitted_subject_round_trips_through_load_subject(tmp_path):
    plan = migrate_config(_config(tmp_path, {"pat": _full_pat()}), apply=True)
    assert plan.subjects[0].warnings == ()  # it loaded back

    subject = load_subject(plan.written[0])
    assert subject.slug == "example-app"
    assert subject.bindings == (PAT_BINDING,)
    assert subject.workspace == Workspace(kind="shared", path=CHECKOUT)
    assert (subject.brief, subject.verify) == ("~/.config/liaise/briefs/pat.md", "npm test")
    assert subject.policy.briefs == {"pat": "~/.config/liaise/briefs/pat.md"}
    assert subject.brief_for("pat") == "~/.config/liaise/briefs/pat.md"
    assert subject.delivery == Delivery(kind="deploy", per="issue", command="./deploy.sh")
    assert subject.label_prefix == "helper:"
    assert subject.processor == ProcessorConfig(permission_mode="acceptEdits")

    policy = subject.policy
    assert policy.people == {"github:pat": "pat"}
    assert policy.roles == {"pat": "partner"}
    assert policy.relays == ("github:owner",)
    assert policy.claim_labels == {"partner:pat": "pat"}
    assert policy.notify == {"pat": "github:octocat"}
    assert (policy.default_reply_mode, policy.reply_modes) == ("direct", {})
    assert policy.readiness == ReadinessPolicy(
        quiet_minutes=15, go_minutes=3, markers=Markers(go="#go#", wait="#hold#")
    )
    assert policy.escalate == EscalateConfig(money_usd=25.0, max_scope="an afternoon")
    assert policy.budget == BudgetPolicy(
        concurrent=1, timeout_minutes=30, max_turns=100, daily_dispatches=4
    )
    assert policy.deployed_nudge_days == 5


# ---- warnings ----


def test_an_empty_deploy_warns(tmp_path):
    plan = migrate_config(_config(tmp_path, {"pat": _partner("pat", deploy="")}))
    (subject,) = plan.subjects
    assert _doc(plan)["delivery"]["command"] == ""
    (warning,) = subject.warnings
    assert warning.startswith("pat (pat.toml): deploy is empty")
    assert f"  warning: {warning}" in plan.lines()


@pytest.mark.parametrize(
    "dispatch, permission_mode, expected",
    [
        (
            {"command": "claude -p {prompt_file} --permission-mode plan"},
            "plan",  # 0.0.x resumed under the mode the command hardcodes
            "dispatch.command is custom and is not carried",
        ),
        (
            {"resume_command": "claude --resume {session_id} -p {prompt_file}"},
            None,
            "dispatch.resume_command is custom and is not carried",
        ),
        (
            {"command": "claude -p {prompt_file}"},
            None,
            "passed no permission mode at all",
        ),
    ],
)
def test_a_custom_command_warns_and_is_not_carried(
    tmp_path, dispatch, permission_mode, expected
):
    fields = _partner("pat", dispatch={"cwd": CHECKOUT, **dispatch})
    plan = migrate_config(_config(tmp_path, {"pat": fields}))
    (subject,) = plan.subjects
    (warning,) = subject.warnings
    assert expected in warning
    assert "{prompt_file}" not in subject.toml_text
    assert _doc(plan).get("processor", {}).get("permission_mode") == permission_mode


def test_a_missing_dispatch_cwd_and_an_unknown_reply_mode_warn(tmp_path):
    fields = _partner("pat", reply_mode="loud")
    del fields["dispatch"]
    plan = migrate_config(_config(tmp_path, {"pat": fields}))
    warnings = " | ".join(plan.subjects[0].warnings)
    assert "reply_mode 'loud'" in warnings and "becomes direct" in warnings
    assert "dispatch.cwd is not set" in warnings
    assert _doc(plan)["policy"]["default_reply_mode"] == "direct"


# ---- bindings are checked, offline ----


def test_binding_problems_are_listed_in_the_plan(tmp_path):
    registry = {
        "github": SimpleNamespace(
            name="github", capabilities=SimpleNamespace(native_fields=("title",))
        )
    }
    plan = migrate_config(_config(tmp_path, {"pat": _partner("pat")}), registry=registry)
    (problem,) = plan.subjects[0].binding_problems
    assert PAT_BINDING in problem and "never matches" in problem
    assert f"  binding problem: {problem}" in plan.lines()


def test_checking_bindings_reaches_no_network_and_runs_no_program(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("migrate-config must not reach the network or run a program")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    plan = migrate_config(_config(tmp_path, {"pat": _partner("pat")}))  # default registry
    assert plan.subjects[0].binding_problems == ()


# ---- roots ----


def test_root_defaults_to_the_config_root(tmp_path, monkeypatch):
    root = _config(tmp_path, {"pat": _partner("pat")})
    monkeypatch.setattr(migrate_module, "DFLT_CONFIG_ROOT", root)
    assert [s.path for s in migrate_config().subjects] == [
        root / "subjects" / "example-app.toml"
    ]


def test_no_partner_files_is_an_empty_plan_that_says_so(tmp_path):
    plan = migrate_config(_config(tmp_path, {}), apply=True)
    assert (plan.subjects, plan.written) == ((), ())
    assert "nothing to migrate" in plan.warnings[0]
    assert plan.lines()[-1] == "wrote 0 subject file(s)"


# ---- the TOML writer ----


def test_dumps_toml_round_trips_every_value_type():
    doc = {
        "text": 'say "hi" \\ back\n\ttab \x01 \x7f é',
        "count": 3,
        "ratio": 0.5,
        "big": 1e20,
        "on": True,
        "off": False,
        "names": ["a", "b"],
        "empty": [],
        "people": InlineTable({"github:pat": "pat", "plain": "x"}),
        "nothing": InlineTable(),
        "skipped": None,
        "policy": {
            "mode": "draft",
            "readiness": {"quiet_minutes": 10, "markers": InlineTable(go="#go#")},
            "unset": {"value": None},
        },
    }
    text = dumps_toml(doc)
    expected = {k: v for k, v in doc.items() if k != "skipped"}
    expected["policy"] = {"mode": "draft", "readiness": doc["policy"]["readiness"]}
    assert tomllib.loads(text) == expected
    assert 'people = { "github:pat" = "pat", plain = "x" }' in text
    assert "\n[policy.readiness]\n" in text
    assert "unset" not in text and "skipped" not in text


def test_dumps_toml_refuses_a_value_toml_cannot_hold():
    with pytest.raises(TypeError, match="not a object"):
        dumps_toml({"thing": object()})
