# terminal-trove-index

A small, independent project that builds a static index of the
[Terminal Trove](https://terminaltrove.com/) catalog for offline use by the
CardputerZero CPZ image.

Terminal Trove has no public catalog API. The collector reads the site's A-Z
list to find tool slugs, then reads each tool page for:

- name, summary and description;
- language, categories, platforms, license and links;
- the install commands Terminal Trove embeds for every supported platform.

The generated `index/trove.json` (and `index/trove.json.gz`) is committed by CI
and can be downloaded by the image build or by Settings → Applications.

## Output

```json
{
  "schema": 1,
  "source": "https://terminaltrove.com",
  "generated_at": "2026-11-17T00:00:00+00:00",
  "count": 1069,
  "categories": [{"slug": "tui", "name": "TUI", "count": 787}],
  "languages": [{"slug": "rust", "name": "Rust", "count": 150}],
  "tools": [
    {
      "slug": "tmux",
      "name": "tmux",
      "url": "https://terminaltrove.com/tmux/",
      "summary": "An open-source terminal multiplexer.",
      "language": "c",
      "categories": ["cli", "tui", "multiplexers"],
      "platforms": ["bsd", "haiku", "linux", "macos"],
      "install": {
        "linux": {"ubuntu / debian": "apt install tmux"},
        "macos": "brew install tmux"
      },
      "apt": {"command": "apt install tmux", "package": "tmux"}
    }
  ]
}
```

`apt` is derived only from the Debian/Ubuntu install cell and is the only field
the CPZ package manager may act on.  Other install commands are displayed to
the user and are never executed.

## Build locally

```bash
python3 scripts/build_index.py --limit 20
python3 scripts/build_index.py --slug tmux --slug htop --refresh
python3 scripts/build_index.py --workers 6
```

The default collector uses `r.jina.ai` because terminaltrove.com is behind a
Cloudflare challenge for non-browser clients.  The free reader tier is rate
limited, so the default delay is 3.2 seconds.  Set `JINA_API_KEY` to use the
authenticated reader tier, or use `--via direct` when running somewhere the
site accepts direct HTML requests.  HTML is cached in `.cache/terminaltrove`
so an interrupted run resumes without re-fetching pages.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Hosting

The workflow in `.github/workflows/update-index.yml` runs daily and can be
started manually.  It rebuilds the index, validates it, and commits changed
`index/` files.  The image consumes:

```text
https://raw.githubusercontent.com/jmas/terminal-trove-index/main/index/trove.json.gz
```

Point the CPZ build at a different repository with `CPZ_TROVE_INDEX_URL`.
The default assumes this project is published as
`github.com/jmas/terminal-trove-index` on the `main` branch.

## Attribution

Catalog metadata is collected from [Terminal Trove](https://terminaltrove.com/)
and remains subject to its terms. The MIT license in this repository covers the
collector code, not the scraped catalog content. The generated index records
the source URL so consumers can attribute it.

## Politeness

The collector identifies itself, caches pages, waits between requests and
retries slowly.  It is intended to run daily, not continuously.  Please keep
the delay conservative.
