# kdenlive-mcp

An MCP server that drives [Kdenlive](https://kdenlive.org) — the KDE video editor.

**It does not need Kdenlive to be running.** A Kdenlive project is an MLT XML
document, so this server reads and writes that XML directly. Everything works
headlessly, on Linux, macOS and Windows, inside WSL, and in CI.

```
┌──────────────┐   MCP/stdio   ┌──────────────────┐   file I/O   ┌─────────────────┐
│  MCP client  │ ────────────► │  kdenlive-mcp    │ ───────────► │ project.kdenlive│
│ (Claude, …)  │ ◄──────────── │  (Python)        │ ◄─────────── │  (MLT XML)      │
└──────────────┘               └──────────────────┘              └─────────────────┘
                                                                    │
                                                          optional  │  melt renders
                                                                    ▼
                                                            output.mp4
```

---

## Why this works when other Kdenlive MCPs don't

Most video-editor MCP servers try to drive the GUI: they simulate clicks,
type into menus, or scrape the window. That approach is fragile — it needs a
display, a specific window layout, and it races against the UI.

Kdenlive publishes its project format, and the format *is* the API:

| Kdenlive concept | Where it lives in the project file |
|---|---|
| Timeline sequence | `<tractor>` carrying a `kdenlive:uuid` |
| Track | `<tractor>` holding one or two `<playlist>` elements |
| Clip | `<entry>` inside a `<playlist>` |
| Gap | `<blank length="…"/>` |
| Media reference | `<chain>` with an `mlt_service` and a `resource` |
| Effect | `<filter>` with an `mlt_service` |
| Transition | `<transition>` with an `mlt_service` |
| Frame rate / size | `<profile>` |

Because the MCP edits that XML directly, every operation is deterministic and
reversible (each save keeps a `.bak`), it never needs a GUI, and it produces
the same result on every operating system.

The layout implemented here is the current format (Kdenlive 23.04+, document
version 1.1) as described in upstream's `dev-docs/fileformat.md`, and it is
verified against Kdenlive's own test projects.

---

## Install

The server is a pure-Python package. [`uv`](https://docs.astral.sh/uv/) is the
recommended installer — it works identically on all three platforms.

### Any platform (uv tool install)

```bash
uv tool install kdenlive-mcp          # from PyPI
uv tool install --from git+https://github.com/jemo69/kdenlive-mcp kdenlive-mcp
```

### Linux

```bash
# from a checkout
uv sync
uv run kdenlive-mcp
```

### macOS

```bash
brew install uv          # if you do not have it
uv sync
uv run kdenlive-mcp
```

### Windows (PowerShell)

```powershell
irm https://astral.sh/uv/install.ps1 | iex    # if you do not have uv
uv sync
uv run kdenlive-mcp
```

### Optional: the renderer

Rendering needs MLT's `melt` command line renderer, which ships with Kdenlive.

| Platform | How to get `melt` |
|---|---|
| Linux | `apt install melt`, or install Kdenlive |
| macOS | `brew install mlt`, or install the Kdenlive app |
| Windows | Install Kdenlive (`C:\Program Files\Kdenlive\bin\melt.exe`) |

If `melt` is somewhere unusual, point the server at it:

```bash
export KDENLIVE_MCP_MELT=/opt/kdenlive/bin/melt
```

Portable builds (AppImages, relocatable installs) may also need extra
environment variables:

```bash
export KDENLIVE_MCP_MELT_ENV="MLT_REPOSITORY=/opt/melt/lib/mlt-7;MLT_DATA=/opt/melt/share/mlt-7"
```

---

## Connect it to an MCP client

The server speaks MCP over stdio. Configuration for the common clients:

### Claude Desktop

`claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`~/.config/Claude/claude_desktop_config.json` on Linux):

```json
{
  "mcpServers": {
    "kdenlive": {
      "command": "uv",
      "args": ["tool", "run", "--from", "kdenlive-mcp", "kdenlive-mcp"],
      "env": {
        "KDENLIVE_MCP_PROJECT": "/home/me/Videos/myproject.kdenlive"
      }
    }
  }
}
```

### OpenCode

`opencode.json`:

```json
{
  "mcp": {
    "kdenlive": {
      "type": "local",
      "command": ["uv", "tool", "run", "--from", "kdenlive-mcp", "kdenlive-mcp"],
      "environment": {
        "KDENLIVE_MCP_PROJECT": "/home/me/Videos/myproject.kdenlive"
      }
    }
  }
}
```

### Anything else

```bash
kdenlive-mcp            # the console script speaks stdio
```

---

## WSL: MCP in Linux, Kdenlive on Windows

This is the configuration the server was designed for.

**Editing works out of the box.** A project on the Windows drive is reachable
from WSL through `/mnt/c`, and since the MCP edits the file directly there is
no boundary to cross:

```bash
# inside WSL
ls /mnt/c/Users/me/Videos/myproject.kdenlive
```

**Rendering and launching the editor need the bridge.** The bridge is a tiny
stdlib-only HTTP server that runs on the Windows side, where Kdenlive lives.

On Windows:

```powershell
python bridge\kdenlive_bridge.py
# Kdenlive bridge listening on http://127.0.0.1:8765
#   kdenlive: C:\Program Files\Kdenlive\bin\kdenlive.exe
#   melt:     C:\Program Files\Kdenlive\bin\melt.exe
```

Then point the MCP at it — inside WSL:

```bash
export KDENLIVE_MCP_BRIDGE_URL=http://172.28.224.1:8765   # the Windows host IP
```

The address is printed by `ip route show default | awk '{print $3}'` inside
WSL. If you are on Windows 11 with mirrored networking, `http://127.0.0.1:8765`
works too; the server tries that address first anyway.

The bridge is also the way to reach a Kdenlive on a *different* machine over
the network — nothing about it is WSL specific.

> The bridge has no authentication. Bind it to `127.0.0.1` (the default) and
> do not expose it to the internet.

---

## Tools

### Inspecting

| Tool | What it does |
|---|---|
| `kdenlive_status` | What this machine can do: OS, `melt`, `ffprobe`, bridge |
| `kdenlive_project_info` | Profile, sequences, tracks and bin clips of a project |
| `kdenlive_sequences` | Every timeline sequence |
| `kdenlive_tracks` | The tracks of a sequence, with mute/lock state |
| `kdenlive_clips` | The clips on a track, with their positions |
| `kdenlive_bin` | The clips in the project bin |
| `kdenlive_effects_on` | The effects and transitions of a clip, track or sequence |
| `kdenlive_effects_catalog` | Every valid `mlt_service` name and its parameters |
| `kdenlive_validate_project` | Structural problems in a project |

### Building

| Tool | What it does |
|---|---|
| `kdenlive_create_project` | A new project with a chosen profile |
| `kdenlive_add_track` | A video or audio track |
| `kdenlive_remove_track` | Remove a track |
| `kdenlive_add_clip` | Place media on the timeline |
| `kdenlive_remove_clip` | Remove a clip, leaving a gap |
| `kdenlive_move_clip` | Move a clip, or move it to another track |
| `kdenlive_trim_clip` | Change a clip's in/out point |
| `kdenlive_split_clip` | Split a clip in two |
| `kdenlive_set_clip_property` | Set properties on a clip |
| `kdenlive_set_track_property` | Rename, lock or mute a track |

### Effects

| Tool | What it does |
|---|---|
| `kdenlive_add_effect` | Add an effect to a clip, a track or the sequence |
| `kdenlive_remove_effect` | Remove an effect |
| `kdenlive_add_transition` | Add a transition between two tracks |
| `kdenlive_remove_transition` | Remove a transition |

### Media and output

| Tool | What it does |
|---|---|
| `kdenlive_probe_media` | Duration, frame rate, resolution and streams of a file |
| `kdenlive_render` | Render the project (runs in the background) |
| `kdenlive_render_status` | Poll a render |
| `kdenlive_render_cancel` | Stop a render |

### Bridge and paths

| Tool | What it does |
|---|---|
| `kdenlive_bridge_status` | Is the bridge reachable? |
| `kdenlive_bridge_projects` | List projects on the bridge machine |
| `kdenlive_bridge_open` | Open a project in the Kdenlive GUI |
| `kdenlive_bridge_render` | Render on the bridge machine |
| `kdenlive_path_translate` | Translate a path between WSL and Windows |

### Units

Positions and durations are **frames** by default. A timecode string such as
`"00:00:05.000"` is accepted anywhere a frame count is. Track, clip and
sequence references accept an index, a name or an id.

---

## Configuration

| Variable | Meaning |
|---|---|
| `KDENLIVE_MCP_PROJECT` | Default project path |
| `KDENLIVE_MCP_MELT` | Path to the `melt` renderer |
| `KDENLIVE_MCP_FFPROBE` | Path to `ffprobe` |
| `KDENLIVE_MCP_BRIDGE_URL` | Base URL of the Kdenlive bridge |
| `KDENLIVE_MCP_MELT_ENV` | Extra `KEY=VALUE` pairs for the `melt` process |
| `KDENLIVE_MCP_BACKUP` | `0` disables the `.bak` file on save |
| `KDENLIVE_MCP_MAX_BACKUPS` | How many `.bak` generations to keep (default 3) |

---

## Development

```bash
uv sync
uv run pytest              # unit tests
uv run ruff check src/
```

The render tests need `melt` and are skipped without it:

```bash
uv run pytest tests/test_render.py
```

`tools/generate_catalog.py` regenerates `src/kdenlive_mcp/_catalog.py` — the
list of effects and transitions — from a Kdenlive source tree:

```bash
python tools/generate_catalog.py /path/to/kdenlive > src/kdenlive_mcp/_catalog.py
```

`tests/fixtures/` holds Kdenlive's own test projects, used to check that this
implementation reads what Kdenlive actually writes.

---

## How the project file is written

Details that matter, all verified against upstream's source:

* **Clip effects live inside the `<entry>`**, not on the producer. The same
  media can be on the timeline more than once and each instance carries its
  own effects.
* **Track effects live inside the track's `<tractor>`**; only the internally
  added audio-mixer effects are attached there as well.
* **Master effects live inside the sequence's `<tractor>`**.
* **The sequence tractor's element id is its uuid**, and the project tractor
  (the last element in the document) references it.
* **Producers are defined before the elements that reference them**, because
  MLT resolves references in document order.
* **Two timecode formats** are in play: `HH:MM:SS.mmm` for MLT's `in`/`out`
  and `HH:MM:SS:FF` for Kdenlive's `kdenlive:duration`.

---

## Licence

MIT. Kdenlive itself is GPL-3.0; this project only reads and writes its
document format and does not link against it.
