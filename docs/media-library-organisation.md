# Proposal: grouping the media library

**Status:** draft for review · **Problem:** picking files for a playlist means
scrolling one flat, unsorted list, and it gets worse with every upload.

## Where the pain actually is

`openPicker()` (`app/static/app.js:593`) renders every file in the library as one
undifferentiated column of cards — no search, no sort, no grouping. Same for the
Media Library grid (`renderMediaGrid()`), the default-content dropdown
(`renderDefaultSelect()`) and Quick play. At 19 files it's annoying; the library
only grows.

Your playlists already *are* the grouping you want — `veidekke`, `bjorvika`,
`12-13` — but there's no way to see the library through that lens while you're
building one.

## What already works

More than you'd expect. The read path is folder-aware today:

| | |
|---|---|
| `list_media()` (`app/media.py:88`) | walks subdirectories via `os.walk`, returns relative paths containing `/` |
| `abs_path()` (`app/media.py:24`) | resolves `client/file.mp4` correctly |
| `thumbnail()` (`app/media.py:65`) | already flattens `/` → `__` for thumb names |
| `previewMedia()` (`app/static/app.js:587`) | already URL-encodes each path segment |
| `/media/<path:rel>` | uses Flask's `path:` converter |

**You can `scp` a folder into `media/` right now and its contents will appear in
the library.** The gap is entirely in *creating and choosing* folders, not in
reading them.

## What's missing

1. **Upload always lands in the root.** `secure_filename()` strips slashes, so
   there's no destination to choose (`app/__init__.py`, `api_upload`).
2. **No folder create / rename / move / delete** anywhere in the UI.
3. **Library grid and picker are flat** — no grouping, search or sort.
4. **No move operation** — see the next section for why that matters.

## The thing to decide first: file identity

Playlists reference files **by path string** (`{"file": "Bjrvika_1_-_16-9_1.mp4"}`).
Move or rename a file and every playlist item pointing at it silently breaks —
so does `default_item`. Any grouping scheme has to answer for that.

This is not hypothetical. It already bites in two places:

- **The transcoder renames.** `_pick_output()` (`app/transcode.py:152`) maps
  `X.mov` → `X.mp4` and `_run()` deletes the original. The moment you run
  → 1080p on `banenor_jernbane_elverum_hovedfilm_16x9.mov`, the `veidekke`
  playlist's reference to it dangles.
- **You already have two dangling references:** `10_Peter_Lang_16x9_Tekstet.mp4`
  (the whole of playlist `12-13`) and `Veidekke_Wilds_Minne_9x16.mp4`. Both point
  at files that aren't on disk. Nothing warns you until playback reaches them.

So whatever we build, **moving a file must rewrite the references that point at
it**, in the same `config.update()` transaction that moves the file.

## Three options

| | A · Folders on disk | B · Tags in config | C · Folders + reference rewriting |
|---|---|---|---|
| Mental model | matches scp/Finder | app-only concept | matches scp/Finder |
| Read path | works today | works today | works today |
| A file in two groups | no | **yes** | no |
| Breaks playlists on move | **yes** | never (paths don't change) | no — rewritten |
| New state to keep in sync | none | tag→file map, orphans on delete | none |
| Visible over SSH | **yes** | no | **yes** |
| Effort | low | medium | medium |

**Option B is tempting** because it sidesteps the identity problem entirely, and
a file *can* belong to both "Veidekke" and "Arendalsuka 2026". But it adds a
parallel source of truth that drifts the moment you touch `media/` over scp — and
you do touch it over scp.

**Recommendation: C, reached in phases**, with B available later as a
cross-cutting layer if one axis proves not to be enough.

## Phase 0 — search and sort (do this first, regardless)

No data model change, no migration, no risk. A filter box and a sort control on
the picker and the library grid:

- filter by substring against the filename, live as you type
- sort by name / newest / size
- keep the filter text between openings of the picker

This is roughly 30 lines in `app.js` and it removes most of the day-to-day pain
on its own. It's also strictly additive — none of it is wasted if folders land
later, since the same filter box then searches within a folder.

**If you only want one thing done, this is the one.**

## Phase 1 — folders

1. **Harden `abs_path()` first.** The current check is
   `p.startswith(config.MEDIA_DIR)`, which lets `../media-evil/x.mp4` through
   (verified). It doesn't matter much while paths are server-generated; it
   matters a lot once they come from the browser. Use
   `os.path.commonpath([p, MEDIA_DIR]) == MEDIA_DIR`.
2. **Upload destination.** A folder dropdown plus "new folder…" on the dropzone;
   sanitise each path segment with `secure_filename` separately rather than the
   whole string.
3. **Folder rail + breadcrumb** in the Media Library and the picker, driven off
   the paths `list_media()` already returns. Files in the root show under
   "Uncategorised".
4. **Move / rename**, as one endpoint that:
   - moves the file,
   - rewrites every matching `file` in every playlist item and `default_item`,
   - renames the thumbnail,
   - all inside a single `config.update()`.
5. **Make the transcoder use it** — `_run()` should go through the same
   reference-rewriting path instead of silently deleting the source, which fixes
   the `.mov` → `.mp4` orphaning as a side effect.
6. **A "broken reference" badge** in the playlist editor for items whose file is
   missing. Cheap, and it would have surfaced your two dangling entries.

## Phase 2 — tags (optional, only if needed)

Free-form tags stored per file in `config.json`, shown as filter chips above the
picker. Worth it only if you find yourself wanting one file in two places. Defer
until that actually happens.

## Proposed starting taxonomy

Client or project at the top level, since that's already how your playlists
split:

```
media/
  Veidekke/
    portretter/                    01_Stian, 02_Trym, 06_Elin, 07_TomKristian,
                                   08_Fillip, 09_Ingvild, 11_Fredrik
    00_Veidekke_presentasjonsfilmV9_16x9_NyLogo.mp4
    251113_veidekke_veidekkekartet_v3_v8__2160p_.mp4
    VEIDEKKE_AUTOMASKINER_57sek_V1_16x9.mp4
    Spot_-_Veidekke_SIA_scan_-_2.mp4
  BaneNOR/
    banenor_jernbane_elverum_hovedfilm_16x9.mov
  Bjorvika/
    Bjrvika_1_-_16-9_1.mp4, Bjrvika_2_-_16-9_1.mp4
    Oen_1_16-9_4K_1.mp4, Oen_2_16-9_4K_1.mp4, Oen_3_16-9_4K_1.mp4
  Lumber/
    Lumber_5_-_Prosjekt_film_2.mp4
  Arendalsuka/
    Stand_Bygg_Arendalsuka_kl._12-13.mp4
```

Two rules that keep this workable:

- **One level deep by default, two at most.** Deep trees are worse than a flat
  list with a search box.
- **Never rename a file that's in a playlist by hand** — use Move, so the
  references follow.

## Side note: your filenames are mangled

`secure_filename()` strips non-ASCII, so **Bjørvika → `Bjrvika`** and
**Øen → `Oen`** on upload. That directly hurts findability — you can't search for
a name that no longer contains the letters you'd type. Worth fixing in the same
pass as the upload destination: keep Unicode letters, strip only path separators
and control characters.

## Suggested order

| | Work | Risk |
|---|---|---|
| 1 | Phase 0 search + sort | none |
| 2 | Broken-reference badge | none |
| 3 | `abs_path()` hardening | none |
| 4 | Move + reference rewriting | medium — touches config |
| 5 | Upload destination + folder UI | low |
| 6 | Unicode-safe filenames | low |
| 7 | Tags | defer |
