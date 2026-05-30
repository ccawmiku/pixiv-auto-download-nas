#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from pixivpy3 import AppPixivAPI
except ImportError as error:  # pragma: no cover - useful for NAS shell runs
    raise SystemExit(
        "Missing dependency: pixivpy3. Install with `python -m pip install -r requirements.txt`."
    ) from error


DEFAULT_CONFIG: dict[str, Any] = {
    "refresh_token_file": "/config/pixiv_refresh_token.txt",
    "database": "/state/pixiv_auto.sqlite3",
    "output_json": "/state/bookmarks_last.json",
    "download_dir": "/downloads",
    "restrict": ["public", "private"],
    "max_pages_per_restrict": 0,
    "request_delay_seconds": 1.0,
    "stop_after_consecutive_seen": 5,
    "stop_marker": {
        "enabled": True,
        "url": "https://www.pixiv.net/artworks/119175141",
    },
    "include_r18": True,
}


@dataclass
class BookmarkArtwork:
    artwork_id: str
    restrict: str
    title: str
    user_id: str
    user_name: str
    user_account: str
    type: str
    page_count: int
    sanity_level: int | None
    x_restrict: int | None
    is_r18: bool
    tags: list[str]
    create_date: str
    image_urls: dict[str, str]
    meta_pages: list[dict[str, Any]]
    raw: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def artwork_id_from_url(value: str) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    match = re.search(r"(?:artworks/|illust_id=)(\d+)", text)
    return match.group(1) if match else ""


def safe_name(value: str, fallback: str = "pixiv") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(" ._")
    return (name or fallback)[:80]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return deep_merge(DEFAULT_CONFIG, json.loads(path.read_text(encoding="utf-8-sig")))


def read_refresh_token(config: dict[str, Any], cli_token: str = "") -> str:
    if cli_token.strip():
        return cli_token.strip()
    env_token = str(__import__("os").environ.get("PIXIV_REFRESH_TOKEN", "")).strip()
    if env_token:
        return env_token
    token_file = Path(config["refresh_token_file"])
    if not token_file.exists():
        raise FileNotFoundError(
            f"refresh token file not found: {token_file}. "
            "Put the token there, pass --refresh-token, or set PIXIV_REFRESH_TOKEN."
        )
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"refresh token file is empty: {token_file}")
    return token


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            create table if not exists artworks (
                artwork_id text primary key,
                restrict_type text not null,
                title text,
                user_id text,
                user_name text,
                user_account text,
                type text,
                page_count integer not null default 0,
                sanity_level integer,
                x_restrict integer,
                is_r18 integer not null default 0,
                tags_json text not null default '[]',
                image_urls_json text not null default '{}',
                meta_pages_json text not null default '[]',
                raw_json text not null default '{}',
                first_seen_at text not null,
                updated_at text not null
            );
            create index if not exists idx_artworks_updated_at on artworks(updated_at);
            create index if not exists idx_artworks_restrict on artworks(restrict_type);
            create index if not exists idx_artworks_r18 on artworks(is_r18);

            create table if not exists runs (
                id integer primary key autoincrement,
                started_at text not null,
                finished_at text,
                status text not null,
                public_count integer not null default 0,
                private_count integer not null default 0,
                total_count integer not null default 0,
                message text
            );
            """
        )


def begin_run(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("insert into runs(started_at, status) values(?, 'running')", (now_iso(),))
        return int(cur.lastrowid)


def finish_run(db_path: Path, run_id: int, status: str, counts: dict[str, int], message: str = "") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            update runs
            set finished_at=?, status=?, public_count=?, private_count=?, total_count=?, message=?
            where id=?
            """,
            (
                now_iso(),
                status,
                counts.get("public", 0),
                counts.get("private", 0),
                counts.get("total", 0),
                message[-2000:],
                run_id,
            ),
        )


def normalize_illust(illust: Any, restrict: str) -> BookmarkArtwork:
    raw = illust if isinstance(illust, dict) else illust.to_dict()
    tags = []
    for tag in raw.get("tags") or []:
        if isinstance(tag, dict):
            name = tag.get("name")
            translated = tag.get("translated_name")
            if name:
                tags.append(str(name))
            if translated:
                tags.append(str(translated))
        elif tag:
            tags.append(str(tag))
    lowered = {tag.lower() for tag in tags}
    x_restrict = raw.get("x_restrict")
    sanity_level = raw.get("sanity_level")
    is_r18 = bool(
        "r-18" in lowered
        or "r18" in lowered
        or "r-18g" in lowered
        or (isinstance(x_restrict, int) and x_restrict >= 1)
        or (isinstance(sanity_level, int) and sanity_level >= 6)
    )
    user = raw.get("user") or {}
    return BookmarkArtwork(
        artwork_id=str(raw.get("id") or ""),
        restrict=restrict,
        title=str(raw.get("title") or ""),
        user_id=str(user.get("id") or ""),
        user_name=str(user.get("name") or ""),
        user_account=str(user.get("account") or ""),
        type=str(raw.get("type") or ""),
        page_count=int(raw.get("page_count") or 0),
        sanity_level=sanity_level if isinstance(sanity_level, int) else None,
        x_restrict=x_restrict if isinstance(x_restrict, int) else None,
        is_r18=is_r18,
        tags=sorted(set(tags)),
        create_date=str(raw.get("create_date") or ""),
        image_urls=raw.get("image_urls") or {},
        meta_pages=raw.get("meta_pages") or [],
        raw=raw,
    )


def upsert_artworks(db_path: Path, artworks: list[BookmarkArtwork]) -> int:
    inserted_or_updated = 0
    with sqlite3.connect(db_path) as conn:
        for item in artworks:
            if not item.artwork_id:
                continue
            conn.execute(
                """
                insert into artworks(
                    artwork_id, restrict_type, title, user_id, user_name, user_account,
                    type, page_count, sanity_level, x_restrict, is_r18, tags_json,
                    image_urls_json, meta_pages_json, raw_json, first_seen_at, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(artwork_id) do update set
                    restrict_type=excluded.restrict_type,
                    title=excluded.title,
                    user_id=excluded.user_id,
                    user_name=excluded.user_name,
                    user_account=excluded.user_account,
                    type=excluded.type,
                    page_count=excluded.page_count,
                    sanity_level=excluded.sanity_level,
                    x_restrict=excluded.x_restrict,
                    is_r18=excluded.is_r18,
                    tags_json=excluded.tags_json,
                    image_urls_json=excluded.image_urls_json,
                    meta_pages_json=excluded.meta_pages_json,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    item.artwork_id,
                    item.restrict,
                    item.title,
                    item.user_id,
                    item.user_name,
                    item.user_account,
                    item.type,
                    item.page_count,
                    item.sanity_level,
                    item.x_restrict,
                    1 if item.is_r18 else 0,
                    json.dumps(item.tags, ensure_ascii=False),
                    json.dumps(item.image_urls, ensure_ascii=False),
                    json.dumps(item.meta_pages, ensure_ascii=False),
                    json.dumps(item.raw, ensure_ascii=False),
                    now_iso(),
                    now_iso(),
                ),
            )
            inserted_or_updated += 1
    return inserted_or_updated


def artwork_exists(db_path: Path, artwork_id: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("select 1 from artworks where artwork_id=?", (artwork_id,)).fetchone()
        return row is not None


def original_image_entries(item: BookmarkArtwork) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    if item.meta_pages:
        for index, page in enumerate(item.meta_pages):
            urls = page.get("image_urls") if isinstance(page, dict) else None
            if isinstance(urls, dict):
                url = urls.get("original") or urls.get("large")
                if url:
                    entries.append((index, str(url)))
    if not entries:
        meta_single = item.raw.get("meta_single_page") or {}
        url = (
            meta_single.get("original_image_url")
            or item.image_urls.get("original")
            or item.image_urls.get("large")
        )
        if url:
            entries.append((0, str(url)))
    return entries


def fetch_artwork_detail(api: AppPixivAPI, item: BookmarkArtwork) -> BookmarkArtwork:
    result = api.illust_detail(item.artwork_id)
    detail = result.get("illust")
    if not detail:
        return item
    return normalize_illust(detail, item.restrict)


def extension_from_url(url: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    return ".jpg"


def download_original_images(
    api: AppPixivAPI,
    db_path: Path,
    artworks: list[BookmarkArtwork],
    download_dir: Path,
    limit_files: int,
    pages_per_artwork: int,
    overwrite: bool = False,
) -> list[dict[str, Any]]:
    image_dir = download_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
            "Referer": "https://www.pixiv.net/",
        }
    )
    downloaded: list[dict[str, Any]] = []
    for item in artworks:
        if item.type == "ugoira":
            print(f"[download] skip ugoira for image test: {item.artwork_id}", flush=True)
            continue
        detail_item = fetch_artwork_detail(api, item)
        upsert_artworks(db_path, [detail_item])
        entries = original_image_entries(detail_item)
        if pages_per_artwork > 0:
            entries = entries[:pages_per_artwork]
        for page_index, url in entries:
            if limit_files > 0 and len(downloaded) >= limit_files:
                return downloaded
            ext = extension_from_url(url)
            if "/img-original/" not in url:
                print(f"[download] warning: original URL missing for {detail_item.artwork_id}; using fallback {url}", flush=True)
            name = f"{detail_item.artwork_id}_p{page_index:02d}_{safe_name(detail_item.title, detail_item.artwork_id)}{ext}"
            target = image_dir / name
            if target.exists() and target.stat().st_size > 0 and not overwrite:
                downloaded.append(
                    {
                        "artwork_id": detail_item.artwork_id,
                        "page": page_index,
                        "path": str(target),
                        "bytes": target.stat().st_size,
                        "skipped_existing": True,
                    }
                )
                print(f"[download] exists {target.name} {target.stat().st_size} bytes", flush=True)
                continue
            tmp = target.with_suffix(target.suffix + ".part")
            with session.get(url, timeout=90, stream=True) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    raise RuntimeError(f"unexpected content-type for {url}: {content_type}")
                with tmp.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=1024 * 512):
                        if chunk:
                            file.write(chunk)
            tmp.replace(target)
            size = target.stat().st_size
            downloaded.append(
                {
                    "artwork_id": detail_item.artwork_id,
                    "page": page_index,
                    "path": str(target),
                    "bytes": size,
                    "skipped_existing": False,
                }
            )
            print(f"[download] saved {target.name} {size} bytes", flush=True)
    return downloaded


def auth_api(refresh_token: str) -> AppPixivAPI:
    api = AppPixivAPI()
    api.auth(refresh_token=refresh_token)
    return api


def get_own_user_id(api: AppPixivAPI, explicit_user_id: str = "") -> str:
    if explicit_user_id:
        return explicit_user_id
    user_id = getattr(api, "user_id", None)
    if user_id:
        return str(user_id)
    auth_result = getattr(api, "auth_result", None)
    if auth_result:
        user = auth_result.get("user") if isinstance(auth_result, dict) else getattr(auth_result, "user", None)
        if user:
            if isinstance(user, dict):
                return str(user.get("id") or "")
            return str(getattr(user, "id", "") or "")
    raise RuntimeError("could not resolve pixiv user_id from refresh token; pass --user-id manually")


def fetch_bookmarks(
    api: AppPixivAPI,
    db_path: Path,
    user_id: str,
    restrict: str,
    max_pages: int,
    delay: float,
    stop_after_seen: int,
    stop_id: str,
) -> list[BookmarkArtwork]:
    collected: list[BookmarkArtwork] = []
    next_qs: dict[str, Any] | None = None
    page = 0
    consecutive_seen = 0
    stop_found = False
    while True:
        page += 1
        if max_pages > 0 and page > max_pages:
            print(f"[{restrict}] reached max_pages={max_pages}", flush=True)
            break
        if next_qs:
            result = api.user_bookmarks_illust(**next_qs)
        else:
            result = api.user_bookmarks_illust(user_id=user_id, restrict=restrict)
        illusts = list(result.get("illusts") or [])
        if not illusts:
            print(f"[{restrict}] page={page} empty; stop", flush=True)
            break
        page_items = [normalize_illust(illust, restrict) for illust in illusts]
        effective_items: list[BookmarkArtwork] = []
        for item in page_items:
            if stop_id and item.artwork_id == stop_id:
                stop_found = True
                print(f"[{restrict}] stop marker found: {stop_id}", flush=True)
                break
            if item.artwork_id and artwork_exists(db_path, item.artwork_id):
                consecutive_seen += 1
            else:
                consecutive_seen = 0
            effective_items.append(item)
            collected.append(item)
        upsert_artworks(db_path, effective_items)
        r18_count = sum(1 for item in effective_items if item.is_r18)
        print(
            f"[{restrict}] page={page} got={len(effective_items)} total={len(collected)} "
            f"r18_on_page={r18_count} consecutive_seen={consecutive_seen}",
            flush=True,
        )
        if stop_found:
            break
        if stop_after_seen > 0 and consecutive_seen >= stop_after_seen:
            print(f"[{restrict}] stop after {consecutive_seen} consecutive already-seen artworks", flush=True)
            break
        next_url = result.get("next_url")
        if not next_url:
            break
        next_qs = api.parse_qs(next_url)
        if delay > 0:
            time.sleep(delay)
    return collected


def copy_example_config(config_path: Path) -> None:
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Fetch Pixiv own bookmark artwork list into SQLite/JSON.")
    parser.add_argument("--config", default="/config/config.json")
    parser.add_argument("--refresh-token", default="", help="Prefer token file/env for normal use.")
    parser.add_argument("--user-id", default="", help="Normally resolved from refresh token.")
    parser.add_argument("--restrict", choices=["public", "private", "both"], default="")
    parser.add_argument("--max-pages", type=int, default=-1, help="Override config max_pages_per_restrict. 0 = no limit.")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--download-images", action="store_true", help="Download original images for fetched artworks.")
    parser.add_argument("--download-limit", type=int, default=5, help="Maximum image files to download for testing. 0 = no limit.")
    parser.add_argument("--pages-per-artwork", type=int, default=1, help="Image pages per artwork for testing. 0 = all pages.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    copy_example_config(config_path)
    config = load_config(config_path)
    db_path = Path(config["database"])
    init_db(db_path)

    refresh_token = read_refresh_token(config, args.refresh_token)
    api = auth_api(refresh_token)
    user_id = get_own_user_id(api, args.user_id)

    if args.restrict == "both":
        restricts = ["public", "private"]
    elif args.restrict:
        restricts = [args.restrict]
    else:
        value = config.get("restrict", ["public", "private"])
        restricts = [value] if isinstance(value, str) else list(value)
    restricts = [item for item in restricts if item in {"public", "private"}]
    if not restricts:
        raise RuntimeError("restrict must include public and/or private")

    max_pages = int(config.get("max_pages_per_restrict", 0) or 0)
    if args.max_pages >= 0:
        max_pages = args.max_pages
    delay = float(config.get("request_delay_seconds", 1.0) or 0)
    stop_after_seen = int(config.get("stop_after_consecutive_seen", 0) or 0)
    stop_id = ""
    stop_marker = config.get("stop_marker") or {}
    if stop_marker.get("enabled", True):
        stop_id = artwork_id_from_url(str(stop_marker.get("url") or ""))
    output_json = Path(args.output_json or config["output_json"])
    output_json.parent.mkdir(parents=True, exist_ok=True)

    run_id = begin_run(db_path)
    counts = {"public": 0, "private": 0, "total": 0}
    all_items: list[BookmarkArtwork] = []
    try:
        print(f"Authenticated Pixiv user_id={user_id}; restrict={restricts}", flush=True)
        for restrict in restricts:
            items = fetch_bookmarks(api, db_path, user_id, restrict, max_pages, delay, stop_after_seen, stop_id)
            counts[restrict] = len(items)
            counts["total"] += len(items)
            all_items.extend(items)
        downloaded_files: list[dict[str, Any]] = []
        if args.download_images:
            downloaded_files = download_original_images(
                api,
                db_path,
                all_items,
                Path(config["download_dir"]),
                args.download_limit,
                args.pages_per_artwork,
                overwrite=args.overwrite,
            )
        data = {
            "fetched_at": now_iso(),
            "user_id": user_id,
            "counts": counts,
            "downloaded_files": downloaded_files,
            "items": [asdict(item) for item in all_items],
        }
        tmp = output_json.with_suffix(output_json.suffix + ".part")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.move(str(tmp), output_json)
        finish_run(db_path, run_id, "done", counts, "ok")
        if args.print_summary:
            r18_total = sum(1 for item in all_items if item.is_r18)
            print(
                json.dumps(
                    {
                        "counts": counts,
                        "r18": r18_total,
                        "downloaded_files": downloaded_files,
                        "output_json": str(output_json),
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    except Exception as error:
        finish_run(db_path, run_id, "failed", counts, str(error))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
