#!/usr/bin/env python3
"""Build a static, machine-readable index of Terminal Trove tools.

Terminal Trove does not expose a public catalog API.  The site is server
rendered, so this script reads the A-Z list for the tool slugs and then reads
each tool page for metadata, language, categories and the install commands that
the site embeds in its ``data-install`` attribute.

The result is a single JSON file (plus a gzip copy) that the CPZ image can
download and search locally without contacting terminaltrove.com at runtime.

Usage:
    python3 scripts/build_index.py
    python3 scripts/build_index.py --limit 20 --delay 1
    python3 scripts/build_index.py --slug tmux --slug htop --refresh
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SITE = "https://terminaltrove.com"
READER = "https://r.jina.ai/"
LIST_PATH = "/list/"
DEFAULT_INDEX = Path("index/trove.json")
DEFAULT_CACHE = Path(".cache/terminaltrove")
USER_AGENT = (
    "terminal-trove-index/1.0 "
    "(+https://github.com/jmas/terminal-trove-index; catalog index builder)"
)

# These tags are platform information, not tool categories.  They are kept in
# the separate ``platforms`` field so the Apps screen can filter the useful
# categories without listing macOS/BSD/etc. as application domains.
PLATFORM_TAGS = {
    "android",
    "bsd",
    "cross-platform",
    "freebsd",
    "haiku",
    "ios",
    "linux",
    "macos",
    "netbsd",
    "openbsd",
    "operating-system",
    "unix",
    "windows",
}

# Terminal Trove uses lower-case slugs.  A few well-known acronyms read badly
# when the slug is title-cased by the fallback.
CATEGORY_LABELS = {
    "ai": "AI",
    "api": "API",
    "api-clients": "API Clients",
    "bpf": "BPF",
    "cli": "CLI",
    "csv": "CSV",
    "dns": "DNS",
    "git": "Git",
    "irc": "IRC",
    "json": "JSON",
    "llm": "LLM",
    "sre": "SRE",
    "sql": "SQL",
    "ssh": "SSH",
    "ssh-apps": "SSH Apps",
    "tcp": "TCP",
    "tui": "TUI",
    "tui-frameworks": "TUI Frameworks",
    "vpn": "VPN",
    "web": "Web",
}

LANGUAGE_LABELS = {
    "assembly": "Assembly",
    "c": "C",
    "clojure": "Clojure",
    "cpp": "C++",
    "csharp": "C#",
    "crystal": "Crystal",
    "dart": "Dart",
    "elixir": "Elixir",
    "fsharp": "F#",
    "go": "Go",
    "haskell": "Haskell",
    "java": "Java",
    "javascript": "JavaScript",
    "julia": "Julia",
    "kotlin": "Kotlin",
    "lua": "Lua",
    "nim": "Nim",
    "nix": "Nix",
    "objective-c": "Objective-C",
    "ocaml": "OCaml",
    "perl": "Perl",
    "php": "PHP",
    "powershell": "PowerShell",
    "python": "Python",
    "r": "R",
    "ruby": "Ruby",
    "rust": "Rust",
    "shell": "Shell",
    "swift": "Swift",
    "typescript": "TypeScript",
    "zig": "Zig",
    "other": "Other",
}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


_rate_lock = threading.Lock()
_next_request = 0.0


def rate_limit(delay: float) -> None:
    """Space request starts across worker threads."""
    global _next_request
    if delay <= 0:
        return
    with _rate_lock:
        now = time.monotonic()
        wait = _next_request - now
        if wait > 0:
            time.sleep(wait)
        _next_request = time.monotonic() + delay


def titleize(slug: str) -> str:
    """Turn a Terminal Trove slug into a readable label."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-"))


def category_label(slug: str) -> str:
    return CATEGORY_LABELS.get(slug, titleize(slug))


def language_label(slug: str) -> str:
    return LANGUAGE_LABELS.get(slug, titleize(slug))


def strip_html(value: str) -> str:
    """Convert the small HTML fragments in Terminal Trove metadata to text."""
    if not value:
        return ""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</(?:li|div)\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def apt_package(command: str) -> str:
    """Return the package name from an apt install command, or an empty string.

    Terminal Trove commands are display strings, not shell scripts that the
    image executes.  This parser only understands the Debian/Ubuntu install
    form (``apt install tmux``, ``sudo apt-get install -y tmux``) and deliberately
    rejects paths, URLs and anything that is not a package name.
    """
    if not command:
        return ""
    text = command.strip()
    # Ignore anything after a shell operator; install commands occasionally
    # carry a second command in the same cell.
    text = re.split(r"[;&|]", text, maxsplit=1)[0].strip()
    match = re.search(
        r"(?:^|\s)(?:sudo\s+(?:-\S+\s+)*)?(?:env\s+\S+\s+)*"
        r"(?:[A-Z_][A-Z0-9_]*=\S+\s+)*apt(?:-get)?\s+"
        r"(?:install|add)\s+(.+)$",
        text,
    )
    if not match:
        return ""
    try:
        import shlex

        tokens = shlex.split(match.group(1))
    except ValueError:
        tokens = match.group(1).split()
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            # Options that take a value.  This keeps ``-t trixie`` from
            # becoming the package name.
            if token in ("-t", "--target-release", "-o"):
                skip_next = True
            continue
        if "/" in token or token.startswith((".", "~")):
            return ""
        name = token.split("=", 1)[0].split(":", 1)[0]
        if re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
            return name
    return ""


def parse_list(text: str) -> list[dict[str, str]]:
    """Parse the A-Z list page into ``{slug, name, summary}`` records."""
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<li class="list-item">\s*<a class="tt-link"[^>]*'
        r'href="/([^"/]+)/"[^>]*>(.*?)</a>\s*-\s*<small>(.*?)</small>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        slug = match.group(1).strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        records.append(
            {
                "slug": slug,
                "name": strip_html(match.group(2)) or slug,
                "summary": strip_html(match.group(3)),
            }
        )
    return records


def parse_json_ld(text: str) -> dict[str, Any]:
    pattern = re.compile(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        graph = payload.get("@graph") if isinstance(payload, dict) else None
        if isinstance(graph, list):
            for item in graph:
                if (
                    isinstance(item, dict)
                    and item.get("@type") == "SoftwareApplication"
                ):
                    return item
        if isinstance(payload, dict) and payload.get("@type") == "SoftwareApplication":
            return payload
    return {}


def parse_install(text: str) -> dict[str, Any]:
    match = re.search(r'data-install="([^"]*)"', text, re.IGNORECASE)
    if not match:
        return {}
    try:
        value = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def install_apt(install: dict[str, Any]) -> dict[str, str]:
    """Extract the Debian/Ubuntu install command from a data-install map."""
    entries: list[tuple[str, str]] = []
    for platform in ("linux", "debian", "ubuntu"):
        value = install.get(platform)
        if isinstance(value, dict):
            entries.extend(
                (str(key), str(command)) for key, command in value.items()
            )
        elif isinstance(value, str):
            entries.append((platform, value))
    for key, command in entries:
        lowered = key.lower()
        if "ubuntu" not in lowered and "debian" not in lowered:
            continue
        package = apt_package(command)
        if package:
            return {"command": command, "package": package}
    return {}


def parse_tool(slug: str, text: str, fallback: dict[str, str]) -> dict[str, Any]:
    """Parse one Terminal Trove tool page."""
    title_match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    title = strip_html(title_match.group(1)) if title_match else ""
    title = re.sub(r"\s+-\s+Terminal Trove\s*$", "", title, flags=re.IGNORECASE)

    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.IGNORECASE | re.DOTALL)
    name = strip_html(h1_match.group(1)) if h1_match else ""
    name = name or fallback.get("name") or title or slug

    description_match = re.search(
        r'<meta\s+name="description"\s+content="([^"]*)"',
        text,
        re.IGNORECASE,
    )
    summary = strip_html(description_match.group(1)) if description_match else ""
    if not summary:
        tagline_match = re.search(
            r'<p[^>]*id="tagline"[^>]*>(.*?)</p>',
            text,
            re.IGNORECASE | re.DOTALL,
        )
        summary = strip_html(tagline_match.group(1)) if tagline_match else ""
    summary = summary or fallback.get("summary", "")

    ld = parse_json_ld(text)
    description = strip_html(str(ld.get("description", ""))) if ld else ""
    if len(description) > 4000:
        description = description[:3997].rstrip() + "..."

    language_match = re.search(
        r'href="/language/([a-z0-9_-]+)/"', text, re.IGNORECASE
    )
    language = language_match.group(1).lower() if language_match else ""

    license_match = re.search(r'title="license:\s*([^"]+)"', text, re.IGNORECASE)
    license_name = license_match.group(1).strip().lower() if license_match else ""

    install = parse_install(text)
    raw_tags = ld.get("applicationCategory") or []
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags = [
        str(tag).strip().lower()
        for tag in raw_tags
        if str(tag).strip()
    ]
    platforms = sorted(
        {
            *(str(key).lower() for key in install),
            *(tag for tag in tags if tag in PLATFORM_TAGS),
        }
    )
    categories = sorted({tag for tag in tags if tag not in PLATFORM_TAGS})

    same_as = ld.get("sameAs") if isinstance(ld, dict) else None
    if isinstance(same_as, str):
        same_as = [same_as]
    links = [str(item).strip() for item in (same_as or []) if str(item).strip()]
    website_match = re.search(
        r'href="(https?://[^"]+)\?ref=terminaltrove"', text, re.IGNORECASE
    )
    website = website_match.group(1) if website_match else (links[0] if links else "")
    source = links[0] if links else website

    stars_match = re.search(
        r'title="([0-9][0-9,]*)\s+stars on GitHub"', text, re.IGNORECASE
    )
    stars = int(stars_match.group(1).replace(",", "")) if stars_match else 0

    release_match = re.search(
        r'title="Latest release[^"]*">\s*<div[^>]*>\s*([^<]+?)\s*</div>',
        text,
        re.IGNORECASE | re.DOTALL,
    )
    release = strip_html(release_match.group(1)) if release_match else ""

    return {
        "slug": slug,
        "name": name,
        "url": SITE + "/" + slug + "/",
        "summary": summary,
        "description": description,
        "language": language,
        "language_label": language_label(language) if language else "",
        "categories": categories,
        "platforms": platforms,
        "install": install,
        "apt": install_apt(install),
        "website": website,
        "source": source,
        "license": license_name,
        "stars": stars,
        "release": release,
    }


def cache_path(cache_dir: Path, url: str, via: str) -> Path:
    digest = hashlib.sha256((via + "\n" + url).encode("utf-8")).hexdigest()[:20]
    return cache_dir / f"{digest}.html"


def fetch(url: str, cache_dir: Path, delay: float, refresh: bool, via: str) -> str:
    """Fetch a page through the Jina reader (or directly), with disk caching."""
    cached = cache_path(cache_dir, url, via)
    if cached.exists() and not refresh:
        return cached.read_text(encoding="utf-8", errors="replace")

    cache_dir.mkdir(parents=True, exist_ok=True)
    request_url = READER + url if via == "reader" else url
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "X-Return-Format": "html",
        # A cached reader response can be a stale Cloudflare challenge.
        "X-No-Cache": "true",
    }
    api_key = os.environ.get("JINA_API_KEY")
    if api_key and via == "reader":
        headers["Authorization"] = "Bearer " + api_key

    last_error = ""
    for attempt in range(1, 5):
        rate_limit(delay)
        log(f"fetch {url}" + (f" (retry {attempt})" if attempt > 1 else ""))
        try:
            request = urllib.request.Request(request_url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            last_error = f"HTTP {error.code}"
            if error.code in (429, 402, 503):
                time.sleep(30 * attempt)
            else:
                time.sleep(3 * attempt)
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = str(error)
            time.sleep(3 * attempt)
            continue
        if "Just a moment..." in body or "<title>Just a moment" in body:
            last_error = "cloudflare challenge"
            time.sleep(10 * attempt)
            continue
        if "<html" not in body.lower():
            last_error = "reader returned non-HTML content"
            time.sleep(5 * attempt)
            continue
        cached.write_text(body, encoding="utf-8")
        return body
    raise RuntimeError(f"cannot fetch {url}: {last_error}")


def build_index(
    index_path: Path,
    cache_dir: Path,
    *,
    delay: float = 3.2,
    refresh: bool = False,
    via: str = "reader",
    limit: int = 0,
    slugs: list[str] | None = None,
    pretty: bool = False,
    workers: int = 1,
) -> dict[str, Any]:
    list_url = SITE + LIST_PATH
    list_html = fetch(list_url, cache_dir, delay, refresh, via)
    entries = parse_list(list_html)
    if not entries:
        raise RuntimeError(
            "the A-Z list did not contain any tools; the page markup may have changed"
        )
    if slugs:
        wanted = {slug.lower() for slug in slugs}
        entries = [entry for entry in entries if entry["slug"].lower() in wanted]
        found = {entry["slug"].lower() for entry in entries}
        missing = sorted(wanted - found)
        if missing:
            log("warning: not present in the A-Z list: " + ", ".join(missing))
    if limit:
        entries = entries[:limit]
    if not entries:
        raise RuntimeError("no tools selected for indexing")
    log(f"{len(entries)} tools to index")

    tools: list[dict[str, Any]] = []
    failures: list[str] = []

    def load_entry(entry: dict[str, str]) -> dict[str, Any]:
        slug = entry["slug"]
        try:
            page = fetch(SITE + "/" + slug + "/", cache_dir, delay, refresh, via)
            return parse_tool(slug, page, entry)
        except Exception as error:  # noqa: BLE001 - one bad page must not kill the index
            log(f"warning: {slug}: {error}")
            return {
                "slug": slug,
                "name": entry["name"],
                "url": SITE + "/" + slug + "/",
                "summary": entry["summary"],
                "description": "",
                "language": "",
                "language_label": "",
                "categories": [],
                "platforms": [],
                "install": {},
                "apt": {},
                "website": SITE + "/" + slug + "/",
                "source": "",
                "license": "",
                "stars": 0,
                "release": "",
                "_failed": True,
            }

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(load_entry, entry): entry for entry in entries}
            for completed, future in enumerate(as_completed(futures), start=1):
                entry = futures[future]
                tool = future.result()
                if tool.pop("_failed", False):
                    failures.append(entry["slug"])
                tools.append(tool)
                log(f"[{completed}/{len(entries)}] {entry['slug']}")
    else:
        for position, entry in enumerate(entries, start=1):
            tool = load_entry(entry)
            if tool.pop("_failed", False):
                failures.append(entry["slug"])
            tools.append(tool)
            log(f"[{position}/{len(entries)}] {entry['slug']}")

    tools.sort(key=lambda item: (item.get("name", "").lower(), item.get("slug", "")))
    failures.sort()

    category_counts: dict[str, int] = {}
    language_counts: dict[str, int] = {}
    for tool in tools:
        for category in tool.get("categories", []):
            category_counts[category] = category_counts.get(category, 0) + 1
        language = tool.get("language", "")
        if language:
            language_counts[language] = language_counts.get(language, 0) + 1

    categories = [
        {"slug": slug, "name": category_label(slug), "count": count}
        for slug, count in sorted(
            category_counts.items(), key=lambda item: (-item[1], category_label(item[0]))
        )
    ]
    languages = [
        {
            "slug": slug,
            "name": language_label(slug),
            "count": count,
        }
        for slug, count in sorted(
            language_counts.items(),
            key=lambda item: (-item[1], language_label(item[0])),
        )
    ]

    index = {
        "schema": SCHEMA_VERSION,
        "source": SITE,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(tools),
        "failures": failures,
        "categories": categories,
        "languages": languages,
        "tools": tools,
    }

    index_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(index, ensure_ascii=False, indent=2 if pretty else None)
    if not pretty:
        text = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    temporary = index_path.with_suffix(index_path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(index_path)

    gzip_path = index_path.with_suffix(index_path.suffix + ".gz")
    with gzip.open(gzip_path, "wt", encoding="utf-8") as handle:
        handle.write(text + "\n")

    if failures:
        log(f"warning: {len(failures)} pages failed: {', '.join(failures)}")
    log(
        f"wrote {index_path} ({len(tools)} tools, "
        f"{len(categories)} categories, {len(languages)} languages)"
    )
    return index


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index",
        type=Path,
        default=DEFAULT_INDEX,
        help=f"output JSON path (default: {DEFAULT_INDEX})",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"HTML cache directory (default: {DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.2,
        help="seconds between requests; the reader's free tier is rate limited",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="ignore cached HTML and fetch every page again",
    )
    parser.add_argument(
        "--via",
        choices=("reader", "direct"),
        default="reader",
        help="fetch through r.jina.ai (default) or directly from the site",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="parallel page requests; the rate limiter still spaces request starts",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="only index the first N tools (useful for development)",
    )
    parser.add_argument(
        "--slug",
        action="append",
        default=[],
        help="only index this tool slug (repeatable)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="write indented JSON (larger index)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        build_index(
            args.index,
            args.cache,
            delay=max(0.0, args.delay),
            refresh=args.refresh,
            via=args.via,
            limit=max(0, args.limit),
            slugs=args.slug,
            pretty=args.pretty,
            workers=max(1, args.workers),
        )
    except Exception as error:  # noqa: BLE001 - CLI boundary
        log(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
