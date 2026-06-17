#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
IMPORT_DIR = ROOT / "imported"
HTML_PATH = ROOT / "index.html"
DATA_PATH = DATA_DIR / "ktt2_posts.json"

BASE_THREAD_URL = "https://www.ktt2.com/fish-alien-32507544"
THREAD_ID = 32507544
AUTHOR_USERNAME = "aeternitatis"
POSTS_PER_PAGE = 25
TIMEOUT = (15, 120)

MEDIA_LINE_RE = re.compile(r"^!\s*(https?://\S+)\s*$")
TEXT_URL_RE = re.compile(r"https?://\S+")
KTT2_SIZE_SUFFIX_RE = re.compile(
    r"(?i)(\.(?:png|jpe?g|gif|webp|svg|bmp)):[^/?#]+"
)


class NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._active_attrs: dict[str, str] | None = None
        self._buffer: list[str] = []
        self.next_data = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        self._active_attrs = {key: value or "" for key, value in attrs}
        self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or self._active_attrs is None:
            return
        if self._active_attrs.get("id") == "__NEXT_DATA__":
            self.next_data = "".join(self._buffer)
        self._active_attrs = None
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._active_attrs is not None:
            self._buffer.append(data)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            )
        }
    )
    return session


def fetch_next_data(session: requests.Session, url: str) -> dict[str, Any]:
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    parser = NextDataParser()
    parser.feed(response.text)
    if not parser.next_data:
        raise RuntimeError(f"Could not find __NEXT_DATA__ for {url}")
    return json.loads(parser.next_data)


def page_url(page_number: int) -> str:
    return BASE_THREAD_URL if page_number == 1 else f"{BASE_THREAD_URL}/{page_number}"


def parse_blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        paragraph = "\n".join(paragraph_lines).strip()
        paragraph_lines.clear()
        if paragraph:
            blocks.append({"type": "text", "text": paragraph})

    for raw_line in text.replace("\r\n", "\n").split("\n"):
        stripped = raw_line.strip()
        media_match = MEDIA_LINE_RE.match(stripped)

        if media_match:
            flush_paragraph()
            blocks.append({"type": "media", "url": normalize_media_url(media_match.group(1))})
            continue

        if stripped == "---":
            flush_paragraph()
            blocks.append({"type": "divider"})
            continue

        if stripped == "":
            flush_paragraph()
            continue

        paragraph_lines.append(raw_line.rstrip())

    flush_paragraph()
    return blocks


def normalize_media_url(url: str) -> str:
    cleaned = url.strip().replace("\u200b", "")
    cleaned = KTT2_SIZE_SUFFIX_RE.sub(r"\1", cleaned)

    parsed = urlparse(cleaned)
    if parsed.netloc.lower() == "punstoppable.com":
        nested = parse_qs(parsed.query).get("url")
        if nested:
            cleaned = unquote(nested[0])

    return cleaned


def youtube_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if host == "youtu.be":
        return parsed.path.lstrip("/").split("/")[0] or None

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v")
            return values[0] if values else None
        if parsed.path.startswith("/embed/") or parsed.path.startswith("/shorts/"):
            parts = [part for part in parsed.path.split("/") if part]
            return parts[1] if len(parts) > 1 else None

    return None


def classify_url_kind(url: str) -> str:
    if youtube_id(url):
        return "youtube"

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".mp4", ".webm", ".mov", ".m4v", ".ogv"}:
        return "video"
    if suffix in {".mp3", ".wav", ".ogg", ".m4a"}:
        return "audio"
    return "download"


def guess_extension(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".mp4", ".webm", ".mov", ".m4v", ".ogv", ".mp3", ".wav", ".ogg", ".m4a"}:
        return suffix

    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
    if guessed == ".jpe":
        return ".jpg"
    if guessed == ".svgz":
        return ".svg"
    return guessed or ".bin"


def kind_from_content_type(content_type: str, fallback_url: str) -> str:
    normalized = content_type.split(";")[0].strip().lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"

    suffix = Path(urlparse(fallback_url).path).suffix.lower()
    if suffix in {".mp4", ".webm", ".mov", ".m4v", ".ogv"}:
        return "video"
    if suffix in {".mp3", ".wav", ".ogg", ".m4a"}:
        return "audio"
    return "image"


def download_media(
    session: requests.Session,
    media_urls: list[str],
    download_media_files: bool,
) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}

    for url in media_urls:
        if url in results:
            continue

        yt_id = youtube_id(url)
        if yt_id:
            results[url] = {
                "kind": "youtube",
                "embed_url": f"https://www.youtube.com/embed/{yt_id}",
                "source_url": url,
            }
            continue

        if not download_media_files:
            results[url] = {"kind": classify_url_kind(url), "src": url, "source_url": url}
            continue

        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        try:
            with session.get(url, stream=True, timeout=TIMEOUT) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                extension = guess_extension(url, content_type)
                filename = f"{digest}{extension}"
                destination = IMPORT_DIR / filename

                if not destination.exists():
                    with destination.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 64):
                            if chunk:
                                handle.write(chunk)

                results[url] = {
                    "kind": kind_from_content_type(content_type, url),
                    "src": f"imported/{filename}",
                    "source_url": url,
                }
        except Exception:
            results[url] = {
                "kind": classify_url_kind(url),
                "src": url,
                "source_url": url,
            }

    return results


def linkify_text(text: str) -> str:
    parts: list[str] = []
    last_index = 0

    for match in TEXT_URL_RE.finditer(text):
        parts.append(html.escape(text[last_index:match.start()]))
        url = match.group(0)
        escaped_url = html.escape(url, quote=True)
        parts.append(
            f'<a href="{escaped_url}" target="_blank" rel="noreferrer">{escaped_url}</a>'
        )
        last_index = match.end()

    parts.append(html.escape(text[last_index:]))
    return "".join(parts).replace("\n", "<br>\n")


def render_block(block: dict[str, str], media_map: dict[str, dict[str, str]]) -> str:
    block_type = block["type"]

    if block_type == "text":
        return f"      <p>{linkify_text(block['text'])}</p>"

    if block_type == "divider":
        return "      <hr>"

    media = media_map.get(block["url"], {"kind": "image", "src": block["url"]})
    escaped_src = html.escape(media.get("src", block["url"]), quote=True)

    if media["kind"] == "youtube":
        embed_url = html.escape(media["embed_url"], quote=True)
        return (
            "      <div class=\"media video-embed\">"
            f"<iframe src=\"{embed_url}\" title=\"Fish Alien video\" "
            "loading=\"lazy\" allow=\"accelerometer; autoplay; clipboard-write; "
            "encrypted-media; gyroscope; picture-in-picture; web-share\" "
            "referrerpolicy=\"strict-origin-when-cross-origin\" allowfullscreen>"
            "</iframe></div>"
        )

    if media["kind"] == "video":
        return (
            "      <div class=\"media\">"
            f"<video controls playsinline preload=\"metadata\" src=\"{escaped_src}\"></video>"
            "</div>"
        )

    if media["kind"] == "audio":
        return (
            f"      <audio controls preload=\"metadata\" src=\"{escaped_src}\"></audio>"
        )

    if media["kind"] == "link":
        return (
            "      <p>"
            f"<a href=\"{escaped_src}\" target=\"_blank\" rel=\"noreferrer\">{escaped_src}</a>"
            "</p>"
        )

    return (
        "      <div class=\"media\">"
        f"<img src=\"{escaped_src}\" alt=\"\" loading=\"lazy\">"
        "</div>"
    )


def build_html(posts: list[dict[str, Any]], media_map: dict[str, dict[str, str]]) -> str:
    lines = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "  <meta charset=\"UTF-8\">",
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">",
        "  <title>&#x1F41F;&#x1F47D;</title>",
        "  <link rel=\"stylesheet\" href=\"style.css\">",
        "</head>",
        "<body>",
        "  <main class=\"stream\">",
        "    <!-- Imported from KTT2 Fish Alien pages 1-20 -->",
    ]

    for post in posts:
        if post["hidden"] or not post["blocks"]:
            continue
        lines.append(
            f"    <section class=\"entry\" data-page=\"{post['page']}\" "
            f"data-post-id=\"{post['id']}\">"
        )
        for block in post["blocks"]:
            lines.append(render_block(block, media_map))
        lines.append("    </section>")

    lines.extend(
        [
            "  </main>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(lines) + "\n"


def extract_posts_for_page(
    session: requests.Session,
    page_number: int,
) -> tuple[list[dict[str, Any]], int]:
    next_data = fetch_next_data(session, page_url(page_number))
    apollo_state = next_data["props"]["apolloState"]
    thread_key = f'Thread:{{"id":{THREAD_ID}}}'
    thread = apollo_state[thread_key]
    total_pages = (thread["postCount"] + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE
    post_ref_key = next(key for key in thread if key.startswith("posts("))
    post_refs = thread[post_ref_key]

    extracted: list[dict[str, Any]] = []
    for index_on_page, ref in enumerate(post_refs, start=1):
        post = apollo_state[ref["__ref"]]
        user_ref = post.get("user", {}).get("__ref")
        if not user_ref:
            continue
        user = apollo_state[user_ref]
        if user.get("name") != AUTHOR_USERNAME:
            continue

        parent = post.get("parent") or {}
        parent_ref = parent.get("__ref")
        parent_id = None
        if parent_ref and ":" in parent_ref:
            _, _, raw_parent_id = parent_ref.partition(":")
            if raw_parent_id.isdigit():
                parent_id = int(raw_parent_id)

        extracted.append(
            {
                "page": page_number,
                "page_order": index_on_page,
                "id": post["id"],
                "created_at": post.get("createdAt"),
                "updated_at": post.get("updatedAt"),
                "hidden": bool(post.get("hidden")),
                "parent_id": parent_id,
                "source_url": f"{page_url(page_number)}#post-{post['id']}",
                "text": post.get("textContent") or "",
                "blocks": parse_blocks(post.get("textContent") or ""),
            }
        )

    return extracted, total_pages


def collect_media_urls(posts: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for post in posts:
        for block in post["blocks"]:
            if block["type"] != "media":
                continue
            url = block["url"]
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def run(last_page: int, download_media_files: bool) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)

    session = build_session()
    all_posts: list[dict[str, Any]] = []
    total_pages_seen = last_page

    for page_number in range(1, last_page + 1):
        posts, total_pages_seen = extract_posts_for_page(session, page_number)
        all_posts.extend(posts)

    media_urls = collect_media_urls(all_posts)
    media_map = download_media(session, media_urls, download_media_files)

    html_output = build_html(all_posts, media_map)
    HTML_PATH.write_text(html_output, encoding="utf-8")
    DATA_PATH.write_text(
        json.dumps(
            {
                "thread_url": BASE_THREAD_URL,
                "thread_id": THREAD_ID,
                "author_username": AUTHOR_USERNAME,
                "page_count_requested": last_page,
                "page_count_seen": total_pages_seen,
                "post_count": len(all_posts),
                "visible_post_count": sum(1 for post in all_posts if not post["hidden"]),
                "hidden_post_count": sum(1 for post in all_posts if post["hidden"]),
                "media_url_count": len(media_urls),
                "posts": all_posts,
                "media": media_map,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "post_count": len(all_posts),
        "visible_post_count": sum(1 for post in all_posts if not post["hidden"]),
        "hidden_post_count": sum(1 for post in all_posts if post["hidden"]),
        "media_url_count": len(media_urls),
        "html_path": str(HTML_PATH),
        "data_path": str(DATA_PATH),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import KTT2 Fish Alien thread pages into a local static page."
    )
    parser.add_argument(
        "--last-page",
        type=int,
        default=20,
        help="Last KTT2 thread page to import.",
    )
    parser.add_argument(
        "--skip-downloads",
        action="store_true",
        help="Keep remote media URLs instead of mirroring non-YouTube files locally.",
    )
    args = parser.parse_args()

    summary = run(last_page=args.last_page, download_media_files=not args.skip_downloads)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
