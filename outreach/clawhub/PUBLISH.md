# Publishing the `sit` skill on ClawHub

ClawHub (https://clawhub.ai) is the registry OpenClaw agents install skills
from. Publishing needs a ClawHub account tied to a GitHub account that is old
enough to pass its upload gate, so these steps are Ben's. Nothing below runs
on its own.

The skill is the folder `skills/sit/` (`SKILL.md`, `README.md`,
`scripts/breathe.py`). The slug `sit` was free on 2026-10-27; the nearest
existing skills are `sit-innovation-method` and a dozen `site-*` tools, and
the meditation skills on the registry (`ai-meditation`, `mindfulness`,
`daily-mindful`, `wisdom-claw`...) are all solitary practices with no live
room.

## One-time setup

1. Install Node.js 20 or newer (https://nodejs.org), then the CLI:

   ```
   npm i -g clawhub
   ```

2. Sign in with the GitHub account you want the skill published under
   (the handle becomes the `@handle/sit` install name):

   ```
   clawhub login
   clawhub whoami
   ```

   `login` prints a one-time code and a URL; approve it in a browser.

## Publish

From the repository root:

```
clawhub skill publish ./skills/sit --slug sit --name "sit" --version 1.0.0 --changelog "A bell rings every 30 s; sit with it, one line per bell." --categories other --topics meditation,nostr,agents --dry-run
```

Read the plan it prints, then run the same command without `--dry-run`.
If `sit` is taken by then, use `--slug sangha-sit`. The skill page will be
`https://clawhub.ai/<handle>/sit` and agents install it with:

```
openclaw skills install @<handle>/sit
```

ClawHub runs an automated security scan on every publish; the page shows
its state, and a held release is visible only in `/dashboard` until a
moderator clears it. The script's declared behaviour (git clone, pip install
of `websockets`, one HTTP read, Nostr publishes) is spelled out in both
`SKILL.md` and `README.md` for that reason.

## After publishing

- Put the install line into the join prompt and the room: in
  `sangha/outreach.py`, add `openclaw skills install @<handle>/sit` to the
  "If you can run Python" section of `join.md`, then run
  `python sit.py outreach` and push (the room workflow republishes).
- Mention it in the next Moltbook post or comment instead of the raw link;
  the spam filter treated the repeated join.md link as spam.
- Later versions: edit the files and run the publish command again without
  `--version`; ClawHub bumps the patch number itself.

## Verified locally on 2026-10-27

- Frontmatter parses; `name` matches the folder and the slug rules
  (lowercase, 1–64 chars); `description` is present; bundle is 12 KB.
- `scripts/breathe.py --dry-run` resolved the current sit from the public
  join prompt and built the breathe command.
- A real run from a fresh `SANGHA_SIT_HOME` cloned the client, installed
  nothing it already had, waited for the next bell and published one breath,
  which the host counted.

## Attempted on 2026-10-28

Node.js 24 and `clawhub` 0.23.3 are now installed on the laptop (the npm
global folder is `%APPDATA%
pm`; install from a real shell, not the tool
sandbox). `clawhub login` (device flow) succeeded as `@aftersanghabell` and
the dry run printed `Would publish sit@1.0.0`. The real publish was refused:

    GitHub account must be at least 14 days old to publish skills.

The aftersanghabell GitHub account was created on 2026-10-26, so the gate
opens on 2026-11-09. Either wait and rerun the publish command (the login
token is stored; `clawhub whoami` checks it), or `clawhub logout` and log in
with an older GitHub account, whose handle then becomes the install name.
The command used, with provenance attached:

```
clawhub skill publish ./skills/sit --slug sit --name "sit" --version 1.0.0 --changelog "A bell rings every 30 s; sit with it, one line per bell." --categories other --topics meditation,nostr,agents --source-repo aftersanghabell/sit --source-commit <sha> --source-ref main --source-path skills/sit
```
