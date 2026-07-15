# cswap cheatsheet

Quick reference for `cswap` (claude-swap), the multi-account switcher for Claude Code. Run `cswap --help` for the authoritative list.

## Setup / adding accounts

```bash
cswap --add-account                      # add the CURRENTLY logged-in account to cswap
cswap --add-account --slot 3             # add it into a specific slot number

cswap --add-token sk-ant-oat01-...       # add by raw OAuth setup-token
cswap --add-token sk-ant-api03-...       # add by managed API key (auto-detected)
cswap --add-token -                      # read token from stdin (pipe-safe)
cswap --add-token                        # omit value -> prompted securely (hidden input)
cswap --add-token ... --slot 3           # into a specific slot
cswap --add-token ... --email me@x.com   # attach a real email label
```

## Switching (shares your single ~/.claude folder)

```bash
cswap --switch                           # rotate to the next account in sequence
cswap --switch --strategy best           # jump to account with the MOST quota left
cswap --switch --strategy next-available # rotate, skipping rate-limited accounts
cswap --switch-to 2                      # switch to account #2
cswap --switch-to user@example.com       # switch by email
```

## Per-terminal (isolated, does NOT change global account)

```bash
cswap run 2                              # run account 2 in THIS terminal only
cswap run 2 -- --resume                 # forward args after '--' to claude
```

## Inspecting state

```bash
cswap --list                             # list all managed accounts
cswap --list --token-status              # + OAuth token expiry state
cswap --status                           # show current active account
cswap --list --json                      # machine-readable (also works with --status/--switch)
```

## Removing / cleanup

```bash
cswap --remove-account 2                 # remove by slot number
cswap --remove-account user@example.com  # remove by email
cswap --purge                            # remove ALL claude-swap data from the system
```

## Backup / migration

```bash
cswap --export backup.cswap              # export accounts to a file
cswap --export -                         # export to stdout
cswap --export backup.cswap --account 2  # export just one account
cswap --export backup.cswap --full       # include full ~/.claude.json (default: oauthAccount only)
cswap --import backup.cswap              # import accounts
cswap --import backup.cswap --force      # overwrite existing accounts on import
cswap --import -                         # import from stdin
```

## Interactive / maintenance

```bash
cswap --tui                              # arrow-key interactive menu
cswap --upgrade                          # self-upgrade to latest PyPI version
cswap --version
cswap --debug --status                   # add --debug to any command for verbose logs
```

## Mental model

- Shared across all accounts: one `~/.claude` folder (settings, `projects/` history, MCP config, todos, plugins).
- Swapped on a switch: only the OAuth credentials (Keychain on macOS) plus the `oauthAccount` block in `~/.claude.json`.
- `--switch` / `--switch-to` / `--tui` perform a global switch against the shared folder.
- `cswap run N` runs an isolated single terminal session without changing the global account.
