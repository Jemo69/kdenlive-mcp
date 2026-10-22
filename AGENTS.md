# AGENTS.md

Notes for coding agents working on this repository.

## What this project is

An MCP server that edits Kdenlive video projects. The core insight: a
`.kdenlive` project is an MLT XML document, so the server edits that XML
directly instead of driving the GUI. Everything except rendering is pure
Python with no external dependencies.

## Ground truth

Do not guess the file format. The authoritative sources are:

* `dev-docs/fileformat.md` in the [KDE/kdenlive](https://github.com/KDE/kdenlive)
  repository — the format documentation.
* `src/doc/kdenlivedoc.cpp` — `DOCUMENTVERSION = 1.1`,
  `DOCUMENTPATCHVERSION = 1`.
* `src/timeline2/model/builders/meltBuilder.cpp` — how the timeline is
  serialised.
* `src/timeline2/model/trackmodel.cpp` — track properties
  (`kdenlive:track_name`, `kdenlive:audio_track`, `kdenlive:trackheight`,
  and the `hide` property: 1 hides video, 2 hides audio).
* `src/timeline2/model/timelineitemmodel.cpp` — `importTrackEffects`, which
  distinguishes internally added audio-mixer effects from user effects.
* `src/effects/effectstack/model/effectstackmodel.cpp` — how effects are
  serialised (`mlt_service`, `kdenlive_id`).
* `data/effects/*.xml` and `data/transitions/*.xml` — every effect and
  transition with its exact parameters. The `tag` attribute is the
  `mlt_service` string.

`tests/fixtures/` contains Kdenlive's own test projects. **Any change to the
project model must keep those files readable**, and the tests assert it.

## Rules that are easy to get wrong

1. **Producers are defined before the elements that reference them.** MLT
   resolves references in document order. New `<chain>` elements are inserted
   *before* the playlist that uses them, not appended at the end.

2. **The sequence tractor's element `id` is its uuid** (`{...}`), not a
   `tractorN` name. `main_bin` and the project tractor reference that uuid.

3. **Clip effects go inside the `<entry>`.** The producer may be shared by
   several entries; each timeline instance carries its own effects.

4. **Track effects go inside the track's `<tractor>`.** The first playlist of
   a track is *not* where user effects belong.

5. **Two timecode formats.** `HH:MM:SS.mmm` is a clock value in
   milliseconds (MLT's `in`/`out`, `blank/@length`); `HH:MM:SS:FF` is a frame
   count (Kdenlive's `kdenlive:duration`). `src/kdenlive_mcp/timecode.py`
   converts between them and the project frame rate.

6. **Ids must be unique across the whole document**, not just among the root
   children. Filters and transitions are nested, so the id generator walks
   the entire tree.

7. **`out` is inclusive.** A clip's length is `out - in + 1`.

8. **The project tractor is the last element** of the document, and carries
   `kdenlive:projectTractor = 1`.

## Working practices

* Run `uv run pytest` and `uv run ruff check src/` before finishing.
* The render tests in `tests/test_render.py` need `melt` and are skipped
  without it. If `melt` is available, run them — they are the only tests that
  prove the output is a real video.
* When changing how a project is written, re-run the checks against
  `tests/fixtures/*.kdenlive` and against `src/kdenlive_mcp/schema.py`, which
  encodes MLT's content models.
* Do not add a runtime dependency for something the standard library can do.
  The MCP must keep working with only Python installed.
* Keep the tool surface small and each tool's return value factual. A tool
  that returns made-up data is worse than no tool at all.
* Tool parameters that accept an index should also accept a name or id, and
  frame counts should also accept a timecode string. FastMCP validates
  strictly, so the annotation has to allow it.

## Regenerating the effect catalogue

```bash
python tools/generate_catalog.py /path/to/kdenlive > src/kdenlive_mcp/_catalog.py
```

Never hand-edit `_catalog.py`; it is generated.
