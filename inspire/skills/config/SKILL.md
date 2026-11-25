---
name: config
version: 1.1.1
description: "Inspire CLI configuration and identity: the layer precedence and why the same key can differ by directory, accounts vs workspaces vs state, where secrets may live, and which config facts decide where work lands. Use when asking what is configured, debugging auth/config errors, switching accounts/workspaces, or scripting env setup. NOT for using jobs/notebooks (their skills) or setting an image default (image skill)."
metadata:
  requires:
    bins: ["inspire"]
  cliHelp: "inspire config --help"
---

# config

**Read the shared skill first (`inspire skills read shared`)**: envelope, exit codes, the `--yes`
gate. Which workspace *means* what (egress, GPU class) is a **platform** skill question; this skill
is about where values come from.

## Platform Rules (what --help cannot tell you)

1. **Four layers, and the winner is printed.** Built-in defaults → global `~/.config/inspire/config.toml`
   (`INSPIRE_GLOBAL_CONFIG_PATH` moves it) → project `./.inspire/config.toml` (found by walking up
   from cwd) → env (`INSPIRE_*`). `config show` prints every value **with its winning source** — when
   a value "isn't what you set", the answer is which layer won, and `show` already names it.

2. **Where you stand decides what applies.** Project config is located by walking up from the
   current directory, so running the same command from a different cwd can silently bind a different
   project config (or none). In scripts, pin identity explicitly (`--account`, `INSPIRE_ACCOUNT`)
   instead of relying on directory context.

3. **Accounts and workspace aliases are local, user-written names.** An account alias maps to one
   platform username (the login one — phone/student-ID/email, not the display name). A workspace
   alias maps to an exact platform display name under that account. Commands accept
   `--workspace <alias|exact-name|id>`, with `--workspace-id <uuid>` as the explicit ID form. An
   alias that resolves on one account means nothing on another; discovery never creates aliases.

4. **Aliases do not describe resources or egress.** GPU class is read from live logical-group
   detail, not names. `workspace list` shows each alias beside the exact workspace name it selects.
   Internet reachability is declared per parent compute pool (`cg-` ID), shared by every logical
   group in that pool. Use `inspire workspace pool set <cg-|lcg-|exact-name> --internet on|off`;
   undeclared means unknown. The **workspace** skill owns pool identity, evidence, and declarations.

5. **Config, state and cache are three different stores.** Config is what you set (the layers
   above). State is what the CLI learned (`~/.local/state/inspire-cli/accounts/<alias>/`, XDG
   state). Cache is a name-resolution accelerator (`inspire cache` inspects/clears it; `cache clear`
   is high-risk — `--yes`). Staleness in the last two is never fixed by editing the first.

6. **Secrets resolve, they are not stored in committed files.** Password material comes from
   `INSPIRE_PASSWORD` or the account's `--password-command`; never hardcode it into a project
   config that gets committed. Output redacts credential-shaped fields — keep it that way.

7. **`config env` is the catalog, `config show` is the reality.** `env` lists what the CLI
   *supports* (with placeholder values); `show` reports what is *set* and from where. Answering one
   with the other sends you chasing variables nobody set.

8. **Removing an account is local-only but total.** `config account remove ALIAS --yes` deletes the
   alias *and its account-scoped local state* (discovery cache, bridges stay); platform-side
   resources are untouched. `account doctor ALIAS --live` resolves identity and state live before
   you conclude an account is broken.

9. **A project can be available in several workspaces.** `project list` JSON carries `workspace_ids`
   for every space whose live project query returned it; the scalar `workspace_id` is only the first
   observation kept for compatibility, not ownership. A project selector's `workspace` is a home
   hint: when targeting another workspace, resolve the name against that target's live project list
   instead of rejecting it on the hint. Never infer project ownership or placement permission from
   either field.

## Applying the rules

**"What is configured, and why?" (1, 7):**

```bash
inspire config show --compact          # only set values, each with its winning source
inspire --json config show | jq
inspire config show -F API             # one category
```

**"Auth is broken" — check before changing (5, 8):**

```bash
inspire config check                   # config + CAS web-session auth (what commands use)
inspire config account doctor <alias> --live
```

**Set values at the right layer (1, 2, 6):**

```bash
inspire config set --dry-run defaults.resource 4xH200    # preview the write
inspire config set defaults.resource 4xH200              # project scope (./.inspire/config.toml)
inspire config set --global defaults.resource 4xH200     # global scope
```

**Second account (3):**

```bash
inspire config account register work --username jane --base-url https://platform.example
inspire --account work job list
```

## Boundaries

- NOT for choosing a workspace on workload grounds (egress, GPU class, quota rows) — **platform**
  skill.
- NOT for image defaults (`image set-default`) — **image** skill.
- NOT for bridge profiles (`~/.inspire/bridges*.json`, `tunnel add`) — **bridge** skill.
