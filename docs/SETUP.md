# Setup

## Requirements

- Python >= 3.11
- For service deployment: systemd with a user manager and logind (linger)

## Foreground / development

See the [README quick start](../README.md#quick-start) (venv, `requirements.txt`, `config.yaml`, `./run.sh`).

`run.sh` automatically uses `.venv/bin/python` when a virtualenv exists in the repo root. The installed console script is `model-switcher` (same flags as `python -m src`).

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
Restart=on-failure
RestartSec=5
Environment=HOME=%h

[Install]
WantedBy=default.target
```

`ExecStart` points at a wrapper in `$HOME/bin` that launches the switcher
(see [CLI Reference](../README.md#cli-reference) for flags).

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
