# liaise.prompt

The prompt composer: the whole prompt a processor run on one case starts from.

[`compose_case_prompt()`](#liaise.prompt.compose_case_prompt) builds it in a fixed order: the packaged operating rules, the
brief for the case’s reporter, the case pointer, the outcome vocabulary, the verify and
delivery commands, and the budget. The processor writes it to a file and puts only a pointer to that
file on the command line. The agent reports only through the structured outcomes its run
ends with: it is never told to post, label or open anything.

### Module Attributes

| [`MODES`](#liaise.prompt.MODES)                    | continuing the case's stored session, because someone wrote again since it last stopped.   |
|---------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| [`OPERATING_RULES_RESOURCE`](#liaise.prompt.OPERATING_RULES_RESOURCE) | The packaged rules every prompt starts with, in `liaise/data`.                             |
| [`GITHUB_ISSUE_URL`](#liaise.prompt.GITHUB_ISSUE_URL)         | GitHub redirects `/issues/N` to the pull request's page when N is a pull request.          |
| [`OUTCOME_GUIDANCE`](#liaise.prompt.OUTCOME_GUIDANCE)         | What each outcome kind is for, as the agent reads it.                                      |

### Functions

| [`compose_case_prompt`](#liaise.prompt.compose_case_prompt)(subject, case, mode, \*)   | Build the prompt for one run on `case`, in the fixed section order.        |
|-------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| [`conversation_link`](#liaise.prompt.conversation_link)(ref)                         | Where the conversation that the encoded reference `ref` names can be read. |

### liaise.prompt.GITHUB_ISSUE_URL *= 'https://github.com/{owner}/{repo}/issues/{number}'*

GitHub redirects `/issues/N` to the pull request’s page when N is a pull request.

### liaise.prompt.MODES *= ('fresh', 'resume')*

continuing the case’s stored session, because someone
wrote again since it last stopped.

* **Type:**
  fresh
* **Type:**
  a new session. resume

### liaise.prompt.OPERATING_RULES_RESOURCE *= 'operating_rules.md'*

The packaged rules every prompt starts with, in `liaise/data`.

### liaise.prompt.OUTCOME_GUIDANCE *= mappingproxy({'ask': 'you need the partner to answer before you can go on. Put the questions in \`questions\`, one per entry and in order, each carrying its suggested default, as in \`Which browsers should this work in? (default: all current ones)\`. \`text\` is an optional lead-in.', 'reply': 'a message to the partner that needs no answer: progress, an explanation, or pushback with a reason and an alternative. Put it in \`text\`.', 'escalate': 'the owner must decide (see the escalation rule). \`text\` is the message to the partner, written so the owner can send it as is; \`reason\` tells the owner why it needs them.', 'propose': "you have a concrete option and want the partner's go-ahead before doing it. Describe it in \`text\`.", 'deliver': "the change has landed (branch, pull request, CI green, verify passing) and is ready for the partner. \`text\` says what changed and what to try, in the partner's terms.", 'decline': 'you think the request should not be done. It is treated as \`escalate\`: the owner decides. Write the message to the partner in \`text\` and your reason in \`reason\`.', 'defer': 'the case should wait, because the partner asked you to or something outside this case has to happen first. \`reason\` says what it waits for.', 'note': 'something for the owner only, never shown to the partner: a problem you noticed, a follow-up worth doing, where you stopped. Put it in \`text\`.'})*

What each outcome kind is for, as the agent reads it. Listed in the order of
[`liaise.model.OUTCOME_KINDS`](liaise.model.html.md#liaise.model.OUTCOME_KINDS), which must all have an entry.

### liaise.prompt.compose_case_prompt(subject, case, mode, , runs_dir_note=None)

Build the prompt for one run on `case`, in the fixed section order.

The sections: the packaged operating rules, the brief for the case’s reporter (see
[`brief_for()`](liaise.subjects.html.md#liaise.subjects.Subject.brief_for)), the case pointer (each conversation, as
a URL where it has one), the outcome vocabulary, the verify and delivery commands,
and the budget. The agent is never told to post, label or open
anything: it reports only through the structured outcomes its run ends with.

`mode` is `"fresh"` or `"resume"`. `runs_dir_note`, when given, is added as
it is to the case section (the tick can say there where this run’s files are kept).
Raises `ValueError` for an unknown mode, and [`ConfigError`](liaise.config.html.md#liaise.config.ConfigError)
for a configured brief that cannot be read.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

### liaise.prompt.conversation_link(ref)

Where the conversation that the encoded reference `ref` names can be read.

A GitHub issue or pull request (`github:owner/repo#N`) becomes its web URL. Any
other reference is returned as it is.

* **Return type:**
  [`str`](https://docs.python.org/3/builtins/stdtypes.html#str)

```pycon
>>> conversation_link("github:example/app#12")
'https://github.com/example/app/issues/12'
>>> conversation_link("webinbox:example-site")
'webinbox:example-site'
```
