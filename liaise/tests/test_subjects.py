"""Tests for liaise.subjects: loading subject files, their defaults, their errors, and helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from correspond.model import Grade

from liaise import subjects as subjects_module
from liaise.config import ConfigError, EscalateConfig, Markers
from liaise.subjects import (
    BudgetPolicy,
    Delivery,
    Policy,
    ProcessorConfig,
    ReadinessPolicy,
    Subject,
    Workspace,
    check_bindings,
    load_subject,
    load_subjects,
)

BINDING = "github:example/app?labels=partner:pat"

MINIMAL_TOML = """
bindings = ["github:example/app?labels=partner:pat"]

[policy]
people = { "github:pat" = "pat" }
roles = { pat = "partner" }
"""

FULL_TOML = """
display_name = "Example app"
bindings = ["github:example/app?labels=partner:pat", "webinbox:example-site"]
workspace = { kind = "shared", path = "~/code/example-app" }
brief = "~/.config/liaise/briefs/pat.md"
verify = "npm test"
delivery = { kind = "pr_only", per = "issue", command = "./deploy.sh" }
label_prefix = "helper:"

[policy]
default_reply_mode = "direct"
reply_modes = { pat = "draft" }
people = { "github:pat" = "pat" }
roles = { pat = "partner" }
relays = ["github:example-bot"]
claim_labels = { "partner:pat" = "pat" }
notify = { pat = "github:pat" }
leak_terms = ["example-internal"]
public_channels = ["github", "webinbox"]

[policy.permissions]
partner = ["report", "request_work"]
observer = ["report"]

[policy.grades]
report = ["platform", "domain", "bound", "crypto"]

[policy.readiness]
quiet_minutes = 15
go_minutes = 3
markers = { go = "#go#", wait = "#hold#" }

[policy.escalate]
money_usd = 25.0
max_scope = "an afternoon"

[policy.budget]
concurrent = 2
timeout_minutes = 30
max_turns = 100
daily_dispatches = 4

[processor]
permission_mode = "acceptEdits"
"""


def _write(root: Path, text: str, *, slug: str = "pat") -> Path:
    path = root / "subjects" / f"{slug}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _load(tmp_path: Path, text: str = MINIMAL_TOML):
    return load_subject(_write(tmp_path, text))


def _config_error(tmp_path: Path, text: str) -> tuple[Path, str]:
    path = _write(tmp_path, text)
    with pytest.raises(ConfigError) as error:
        load_subject(path)
    return path, str(error.value)


# ---- loading ----


def test_minimal_subject_gets_every_default(tmp_path):
    path = _write(tmp_path, MINIMAL_TOML)
    subject = load_subject(path)
    assert subject.slug == "pat"
    assert subject.display_name == "pat"
    assert subject.bindings == (BINDING,)
    assert subject.workspace == Workspace(kind="shared", path="")
    assert (subject.brief, subject.verify) == ("", "")
    assert subject.delivery == Delivery(kind="deploy", per="batch", command="")
    assert subject.label_prefix == "liaise:"
    assert subject.processor == ProcessorConfig(permission_mode="auto")
    assert subject.source == str(path)

    policy = subject.policy
    assert policy.people == {"github:pat": "pat"}
    assert policy.roles == {"pat": "partner"}
    assert policy.default_reply_mode == "draft"
    assert policy.reply_modes == {}
    assert policy.relays == ()
    assert policy.claim_labels == {}
    assert policy.notify == {}
    assert policy.leak_terms == ()
    assert policy.public_channels == ("github",)
    assert policy.permissions == {
        "partner": ("report", "request_work", "approve_candidate"),
        "observer": ("report",),
    }
    assert policy.grades == {
        "report": ("claimed", "platform", "domain", "bound", "crypto"),
        "request_work": ("platform", "domain", "bound", "crypto"),
        "approve_candidate": ("platform", "domain", "bound", "crypto"),
    }
    assert policy.readiness == ReadinessPolicy(
        quiet_minutes=10, go_minutes=2, markers=Markers(go="#startwork#", wait="#wait#")
    )
    assert policy.escalate == EscalateConfig(money_usd=50.0, max_scope="about a day of work")
    assert policy.budget == BudgetPolicy(
        concurrent=1, timeout_minutes=60, max_turns=200, daily_dispatches=6
    )


def test_full_subject_file_reads_every_value(tmp_path):
    subject = _load(tmp_path, FULL_TOML)
    assert subject.display_name == "Example app"
    assert subject.bindings == (BINDING, "webinbox:example-site")
    assert subject.workspace == Workspace(kind="shared", path="~/code/example-app")
    assert subject.brief == "~/.config/liaise/briefs/pat.md"
    assert subject.verify == "npm test"
    assert subject.delivery == Delivery(kind="pr_only", per="issue", command="./deploy.sh")
    assert subject.label_prefix == "helper:"
    assert subject.processor == ProcessorConfig(permission_mode="acceptEdits")

    policy = subject.policy
    assert policy.default_reply_mode == "direct"
    assert policy.reply_modes == {"pat": "draft"}
    assert policy.relays == ("github:example-bot",)
    assert policy.claim_labels == {"partner:pat": "pat"}
    assert policy.notify == {"pat": "github:pat"}
    assert policy.leak_terms == ("example-internal",)
    assert policy.public_channels == ("github", "webinbox")
    assert policy.permissions["partner"] == ("report", "request_work")
    assert policy.grades["report"] == ("platform", "domain", "bound", "crypto")
    assert policy.readiness == ReadinessPolicy(
        quiet_minutes=15, go_minutes=3, markers=Markers(go="#go#", wait="#hold#")
    )
    assert policy.escalate == EscalateConfig(money_usd=25.0, max_scope="an afternoon")
    assert policy.budget == BudgetPolicy(
        concurrent=2, timeout_minutes=30, max_turns=100, daily_dispatches=4
    )


def test_partial_tables_keep_the_other_defaults(tmp_path):
    text = MINIMAL_TOML + (
        '\n[policy.permissions]\ntester = ["report"]\n'
        '\n[policy.grades]\nreport = ["crypto"]\n'
        '\n[policy.readiness]\nmarkers = { go = "#go#" }\n'
    )
    policy = _load(tmp_path, text).policy
    assert policy.permissions["tester"] == ("report",)
    assert policy.permissions["partner"] == ("report", "request_work", "approve_candidate")
    assert policy.grades["report"] == ("crypto",)
    assert policy.grades["request_work"] == ("platform", "domain", "bound", "crypto")
    assert policy.readiness.markers == Markers(go="#go#", wait="#wait#")
    assert policy.readiness.quiet_minutes == 10


def test_load_subjects_keys_by_slug_in_slug_order(tmp_path):
    _write(tmp_path, MINIMAL_TOML.replace("example/app", "example/site"), slug="zeta")
    _write(tmp_path, MINIMAL_TOML, slug="pat")
    subjects = load_subjects(tmp_path)
    assert list(subjects) == ["pat", "zeta"]
    assert subjects["zeta"].slug == "zeta"


def test_load_subjects_without_a_subjects_directory_is_empty(tmp_path):
    assert load_subjects(tmp_path) == {}


def test_load_subjects_defaults_to_the_config_root(tmp_path, monkeypatch):
    _write(tmp_path, MINIMAL_TOML)
    monkeypatch.setattr(subjects_module, "DFLT_CONFIG_ROOT", tmp_path)
    assert list(load_subjects()) == ["pat"]


# ---- errors name the file and the fix ----


@pytest.mark.parametrize(
    "text, expected",
    [
        (MINIMAL_TOML.replace(f'bindings = ["{BINDING}"]', ""), "missing required field 'bindings'"),
        (f'bindings = ["{BINDING}"]\n', "missing required field 'policy.people'"),
        (
            f'bindings = ["{BINDING}"]\n[policy]\nroles = {{ pat = "partner" }}\n',
            "missing required field 'policy.people'",
        ),
        (
            f'bindings = ["{BINDING}"]\n[policy]\npeople = {{ "github:pat" = "pat" }}\n',
            "missing required field 'policy.roles'",
        ),
        (MINIMAL_TOML.replace(f'["{BINDING}"]', "[]"), "bindings is empty"),
    ],
)
def test_missing_required_fields_name_the_file_and_the_minimal_content(tmp_path, text, expected):
    path, message = _config_error(tmp_path, text)
    assert str(path) in message
    assert expected in message
    assert "A minimal subject file needs at least" in message


@pytest.mark.parametrize(
    "extra, expected",
    [
        ('default_reply_mode = "direkt"', "policy.default_reply_mode has 'direkt'"),
        ('reply_modes = { pat = "loud" }', "policy.reply_modes has 'loud'"),
        ('relays = "github:example-bot"', "policy.relays must be a list of strings"),
        ('people = ["github:pat"]', "policy.people must be a table"),
        (
            'claim_labels = { "partner:ada-lovelace" = "ada-lovelace" }',
            "who has no entry in policy.roles",
        ),
        ('\n[policy.permissions]\npartner = ["report", "deploy"]', "policy.permissions.partner has 'deploy'"),
        ('\n[policy.grades]\nreport = ["verified"]', "policy.grades.report has 'verified'"),
        ('\n[policy.grades]\nmerge = ["platform"]', "policy.grades has 'merge'"),
        ('briefs = { sam = "~/.config/liaise/briefs/sam.md" }', "policy.briefs has a brief for 'sam'"),
        ("briefs = { pat = 1 }", "every value in policy.briefs must be a string"),
    ],
)
def test_invalid_policy_values_name_the_file_and_the_fix(tmp_path, extra, expected):
    text = MINIMAL_TOML.replace('people = { "github:pat" = "pat" }\n', "") if extra.startswith("people") else MINIMAL_TOML
    path, message = _config_error(tmp_path, text + extra + "\n")
    assert str(path) in message
    assert expected in message


def test_role_missing_from_permissions_names_the_known_roles(tmp_path):
    path, message = _config_error(tmp_path, MINIMAL_TOML.replace('"partner"', '"tester"'))
    assert str(path) in message
    assert "the role 'tester', which policy.permissions does not define" in message
    assert "observer, partner" in message


@pytest.mark.parametrize(
    "top, expected",
    [
        ('delivery = { kind = "ftp" }', "delivery.kind has 'ftp'"),
        ('delivery = { per = "case" }', "delivery.per has 'case', which is not one of: batch, issue"),
        ('workspace = { kind = "worktree" }', "workspace.kind has 'worktree'"),
        ('delivery = "deploy"', "delivery must be a table"),
    ],
)
def test_invalid_top_level_values_name_the_file(tmp_path, top, expected):
    path, message = _config_error(tmp_path, top + "\n" + MINIMAL_TOML)
    assert str(path) in message
    assert expected in message


def test_bindings_must_be_a_list_of_strings(tmp_path):
    _, message = _config_error(tmp_path, MINIMAL_TOML.replace(f'["{BINDING}"]', f'"{BINDING}"'))
    assert "bindings must be a list of strings" in message


def test_invalid_toml_names_the_file_and_the_quoting_fix(tmp_path):
    text = MINIMAL_TOML.replace('{ "github:pat" = "pat" }', '{ github:pat = "pat" }')
    path, message = _config_error(tmp_path, text)
    assert str(path) in message
    assert "not valid TOML" in message
    assert "must be quoted" in message


def test_missing_file_names_the_path(tmp_path):
    path = tmp_path / "subjects" / "nobody.toml"
    with pytest.raises(ConfigError, match="Missing subject file") as error:
        load_subject(path)
    assert str(path) in str(error.value)


# ---- helpers ----


def test_reply_mode_for_uses_the_person_override_else_the_default(tmp_path):
    subject = _load(tmp_path, FULL_TOML)
    assert subject.reply_mode_for("pat") == "draft"
    assert subject.reply_mode_for("ada-lovelace") == "direct"
    assert subject.reply_mode_for(None) == "direct"


def test_permissions_for_a_role(tmp_path):
    subject = _load(tmp_path)
    assert subject.permissions_for("partner") == ("report", "request_work", "approve_candidate")
    assert subject.permissions_for("observer") == ("report",)
    assert subject.permissions_for("tester") == ()
    assert subject.permissions_for(None) == ()


def test_accepts_checks_the_grade_against_the_permission(tmp_path):
    subject = _load(tmp_path)
    assert subject.accepts("report", Grade.CLAIMED)
    assert subject.accepts("report", "claimed")
    assert not subject.accepts("request_work", Grade.CLAIMED)
    assert subject.accepts("request_work", Grade.PLATFORM)
    assert not subject.accepts("report", Grade.FORGED)


def test_accepts_refuses_an_unknown_permission(tmp_path):
    with pytest.raises(ValueError, match="permission 'merge'"):
        _load(tmp_path).accepts("merge", Grade.CRYPTO)


def _registry(**native_fields):
    """A stand-in correspond registry: only what check_binding reads, so no adapter runs."""
    return {
        channel: SimpleNamespace(name=channel, capabilities=SimpleNamespace(native_fields=fields))
        for channel, fields in native_fields.items()
    }


def test_check_bindings_is_empty_when_every_binding_can_match(tmp_path):
    subject = _load(tmp_path, FULL_TOML)
    registry = _registry(github=("number", "title", "labels", "state"), webinbox=None)
    assert check_bindings(subject, registry=registry) == []


def test_check_bindings_names_the_file_the_binding_and_the_problem(tmp_path):
    path = _write(tmp_path, MINIMAL_TOML.replace("?labels=", "?label="))
    (problem,) = check_bindings(load_subject(path), registry=_registry(github=("labels",)))
    assert str(path) in problem
    assert "github:example/app?label=partner:pat" in problem
    assert "never matches" in problem


def test_check_bindings_reports_an_unknown_channel(tmp_path):
    subject = _load(tmp_path, FULL_TOML)
    (problem,) = check_bindings(subject, registry=_registry(github=("labels",)))
    assert "webinbox:example-site" in problem


# ---- S2 additions: deployed_nudge_days, notify addresses, case-insensitive people ----


def test_deployed_nudge_days_defaults_to_three_and_reads_a_value(tmp_path):
    assert _load(tmp_path).policy.deployed_nudge_days == 3
    assert _load(tmp_path, MINIMAL_TOML + "deployed_nudge_days = 5\n").policy.deployed_nudge_days == 5


@pytest.mark.parametrize("value", ["0", "-1", '"3"', "2.5", "true"])
def test_deployed_nudge_days_must_be_a_whole_number_of_at_least_one(tmp_path, value):
    path, message = _config_error(tmp_path, MINIMAL_TOML + f"deployed_nudge_days = {value}\n")
    assert str(path) in message
    assert "policy.deployed_nudge_days must be a whole number of days, 1 or more" in message


def test_people_addresses_that_differ_only_in_case_must_name_one_person(tmp_path):
    people = 'people = { "github:pat" = "pat" }'
    conflicting = MINIMAL_TOML.replace(people, 'people = { "github:pat" = "pat", "github:PAT" = "someone-else" }')
    path, message = _config_error(tmp_path, conflicting)
    assert str(path) in message
    assert "without regard to case" in message
    agreeing = MINIMAL_TOML.replace(people, 'people = { "github:pat" = "pat", "github:PAT" = "pat" }')
    assert _load(tmp_path, agreeing).policy.people == {"github:pat": "pat", "github:PAT": "pat"}


def _subject_with(**policy) -> Subject:
    return Subject(slug="pat", bindings=(BINDING,), policy=Policy(**{"people": {}, "roles": {}, **policy}))


def test_notify_address_for_prefers_policy_notify():
    subject = _subject_with(people={"github:pat": "pat"}, notify={"pat": "webinbox:pat"})
    assert subject.notify_address_for("pat") == "webinbox:pat"
    assert subject.notify_address_for("pat", channels="github") == "github:pat"


def test_notify_address_for_falls_back_to_the_first_handle_of_the_person():
    subject = _subject_with(people={"github:example-bot": "bot", "webinbox:u-1": "pat", "github:pat": "pat"})
    assert subject.notify_address_for("pat") == "webinbox:u-1"
    assert subject.notify_address_for("pat", channels=("github",)) == "github:pat"
    assert subject.notify_address_for("someone-else") is None
    assert subject.notify_address_for("pat", channels="email") is None


def test_notify_addresses_for_put_the_notify_override_first_then_the_handles():
    people = {"github:pat": "pat", "github:someone-else": "someone-else", "telegram:@pat": "pat", "pat": "pat"}
    subject = _subject_with(people=people, notify={"pat": "github:pat-reports"})
    assert subject.notify_addresses_for("pat") == ("github:pat-reports", "github:pat", "telegram:@pat")
    assert subject.notify_addresses_for("pat", channels=("telegram",)) == ("telegram:@pat",)
    assert subject.notify_addresses_for("pat", channels="telegram") == ("telegram:@pat",)
    assert subject.notify_addresses_for("nobody") == ()
    # a string with no channel is not an address, even as the only one
    assert _subject_with(people={"pat": "pat"}).notify_address_for("pat") is None


def test_one_channel_is_not_read_as_its_letters():
    subject = _subject_with(people={"g:pat": "pat", "github:pat": "pat"})
    assert subject.notify_address_for("pat", channels="github") == "github:pat"


# ---- S2b: per-person briefs, and one subject per polled conversation ----


def test_policy_briefs_default_to_empty_and_read_a_table(tmp_path):
    assert _load(tmp_path).policy.briefs == {}
    text = MINIMAL_TOML + 'briefs = { pat = "~/.config/liaise/briefs/pat.md" }\n'
    assert _load(tmp_path, text).policy.briefs == {"pat": "~/.config/liaise/briefs/pat.md"}


def test_brief_for_prefers_the_persons_brief_then_the_subjects_then_none():
    subject = Subject(
        slug="example-app",
        bindings=(BINDING,),
        policy=Policy(
            people={},
            roles={"pat": "partner", "sam": "partner"},
            briefs={"pat": "~/.config/liaise/briefs/pat.md"},
        ),
        brief="~/.config/liaise/briefs/example-app.md",
    )
    assert subject.brief_for("pat") == "~/.config/liaise/briefs/pat.md"
    assert subject.brief_for("sam") == "~/.config/liaise/briefs/example-app.md"
    assert subject.brief_for(None) == "~/.config/liaise/briefs/example-app.md"
    assert replace(subject, brief="").brief_for("sam") is None


@pytest.mark.parametrize("other_binding", ["github:example/app?labels=partner:sam", "github:Example/App"])
def test_load_subjects_refuses_two_subjects_polling_one_conversation(tmp_path, other_binding):
    first = _write(tmp_path, MINIMAL_TOML, slug="pat")
    second = _write(tmp_path, MINIMAL_TOML.replace(BINDING, other_binding), slug="sam")
    with pytest.raises(ConfigError) as error:
        load_subjects(tmp_path)
    message = str(error.value)
    assert str(first) in message and str(second) in message
    assert "both poll" in message and "one cursor per conversation" in message


def test_several_bindings_of_one_subject_may_share_a_conversation(tmp_path):
    three = f'["{BINDING}", "github:example/app?author=pat", "github:example/*"]'
    _write(tmp_path, MINIMAL_TOML.replace(f'["{BINDING}"]', three), slug="pat")
    _write(tmp_path, MINIMAL_TOML.replace("example/app", "example/site"), slug="sam")
    _write(tmp_path, MINIMAL_TOML.replace(BINDING, "github:example/*"), slug="zeta")  # a glob is never polled
    assert list(load_subjects(tmp_path)) == ["pat", "sam", "zeta"]


# ---- S5b-2: a GitHub binding loads lower-cased, its conditions as written ----


def test_a_github_binding_loads_lower_cased_and_keeps_its_conditions(tmp_path):
    """correspond compares a binding's conversation case-sensitively with the lower-cased
    reference its GitHub adapter gives each message, so `github:Example/App` would match no
    issue at all. Loading lower-cases the channel and conversation; the conditions stay."""
    from correspond.routing import binding_matches

    from liaise.testing import FakeGitHubChannel

    bindings = '["GitHub:Example/App?labels=Partner:Pat", "github:Example/App#12", "webinbox:Example-Site"]'
    subject = _load(tmp_path, MINIMAL_TOML.replace(f'["{BINDING}"]', bindings))

    assert subject.bindings == (
        "github:example/app?labels=Partner:Pat",
        "github:example/app#12",
        "webinbox:Example-Site",
    )
    message = FakeGitHubChannel().add_issue(
        "example/app", 12, author="pat", title="Export", body="", labels=["Partner:Pat"],
        created_at="2026-09-11T09:00:00Z",
    )
    assert binding_matches(subject.bindings[0], message)
