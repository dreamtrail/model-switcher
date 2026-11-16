# Setup

## Requirements

- Python >= 3.11
- For service deployment: systemd with a user manager and logind (linger)

## Foreground / development

See the [README quick start](../README.md#quick-start) (venv, `requirements.txt`, `config.yaml`, `./run.sh`).

`run.sh` automatically uses `.venv/bin/python` when a virtualenv exists in the repo root. The installed console script is `model-switcher` (same flags as `python -m src`).

## Shutdown-hook stop budgets

Optional `shutdown_cmd` and `shutdown_timeout` are set per model, or per
variant for [variant models](CONFIGURATION.md#model-variants), and are described in
the [configuration guide](CONFIGURATION.md#optional-shutdown-hooks). Supply your own
local executable; this project does not provide remote scripts or SSH setup.
The service's environment, working directory, PATH, and permissions apply to
hooks. Use absolute executable paths where appropriate and arrange credentials
outside command arguments. Hooks must never prompt for input.

Budget service stop time for the hook timeout (default 30s), plus hook process
group cleanup (up to 3s SIGTERM grace and 5s SIGKILL confirmation), plus the same
local launcher cleanup budget (with the model's `stop_grace`, default 3s, as its
SIGTERM grace), plus server/request shutdown overhead. Configure
systemd `TimeoutStopSec` and any external supervisor's deadlines accordingly;
a hook timeout is not the total switcher shutdown deadline. Hook descendants
must stay in their process group; daemonized/detached workers cannot be tracked
by local group cleanup. Scripts must explicitly confirm any remote termination.

Hook failure does not skip local cleanup, but retains a failed lifecycle and
blocks replacement. A later unload, load, or shutdown attempt retries unfinished
stages only. Cancellation waits for owned cleanup and does not start overlapping
hooks. The CLI's second-signal cancellation policy is unchanged; it is not an
immediate-exit guarantee. SIGKILL, power loss, network failure, or forced service
termination can leave workers running. There is no persisted cleanup journal or
recovery across a switcher restart; arrange independent operational recovery.

## Running as a systemd user service

The production deployment (host `ai`) runs the switcher as a **user** unit at
`~/.config/systemd/user/llm-server.service`:

```ini
[Unit]
Description=LLM Server
After=network.target

[Service]
Type=simple
ExecStart=%h/bin/llm-server
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
Environment=HOME=%h

[Install]
WantedBy=default.target
```

`ExecStart` points at a wrapper in `$HOME/bin` that launches the switcher
(see [CLI Reference](CLI.md#cli-reference) for flags).

### Reload the config with systemctl

`systemctl --user reload llm-server.service` sends SIGHUP to the switcher. It
re-reads its config file and applies the changes without a restart, the same as
the control API's reload ([what changes and what does not](CONTROL.md#reloading-the-config)).
Once the switcher is running it never exits on SIGHUP. A SIGHUP during startup
(before it logs `Listening on`) still terminates it. An invalid config keeps the
old one and logs a warning; check the result with
`journalctl --user -u llm-server.service -n 20`.

The wrapper must `exec` the switcher as its last command (for example
`exec ~/model-switcher/run.sh --config ~/models.yaml`). Without `exec`,
`$MAINPID` is the wrapper shell, and SIGHUP goes to the shell instead of the
switcher. A bash wrapper is killed by SIGHUP (it exits with status 129), so
systemd sees the service exit and `Restart=on-failure` would restart it, which
reloads nothing and drops the loaded model. The bash part was checked; what
systemd then does was not tested.

### Linger is required (read first)

User units run under the **user manager** (`systemd --user`), which is tied to
login-session lifetime. When the last session ends — logout, **SSH
disconnect**, or an idle-session cutoff — the user manager stops, and every
user unit dies with it, including a model mid-load (wasting load time and
VRAM, resetting in-flight requests). When you reconnect, the units come back.
The journal looks like this even though nobody ran `systemctl stop`:

```
Stopping llm-server.service - LLM Server...
...
Started llm-server.service - LLM Server.     # seconds later, on reconnect
```

`Restart=on-failure` cannot help here: the manager that would restart the
unit is itself shutting down.

Enable **linger** to decouple the user manager from session lifetime. It then
survives logouts and starts at boot:

```bash
sudo loginctl enable-linger <user>
loginctl show-user <user> | rg -i Linger    # expect Linger=yes
```

With linger, the service also comes up at boot/container start (the unit is
enabled via `WantedBy=default.target`) without anyone being logged in.

### Enable and verify

```bash
systemctl --user enable --now llm-server.service
systemctl --user status llm-server.service
journalctl --user -u llm-server.service -f
```

Disconnect test: drop the SSH session, re-login, then check
`journalctl --user -u llm-server.service --since "10 min ago"` — there must be
**no** `Stopping` entry after the disconnect.
