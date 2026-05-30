#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import requests
from pixivpy3 import AppPixivAPI


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

APP_NAME = "Pixiv Auto Downloader"
DEFAULT_CONFIG_PATH = Path("/config/config.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

DEFAULT_CONFIG: dict[str, Any] = {
    "run_interval_hours": 12,
    "run_interval_seconds": 43200,
    "refresh_token_file": "/config/pixiv_refresh_token.txt",
    "database": "/state/pixiv_auto.sqlite3",
    "download_dir": "/downloads",
    "image_dir": "/downloads/images",
    "metadata_dir": "/downloads/downloads-metadata",
    "restrict": ["public", "private"],
    "max_pages_per_restrict": 0,
    "request_delay_seconds": 1.0,
    "download_delay_seconds": 1.0,
    "retry_failed": True,
    "max_download_attempts": 0,
    "stop_after_consecutive_done": 5,
    "stop_marker": {
        "enabled": True,
        "url": "https://www.pixiv.net/artworks/119175141",
    },
    "media": {
        "download_images": True,
        "download_ugoira": True,
        "ugoira_format": "gif",
        "ugoira_fps_fallback": 12,
    },
    "web": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 8080,
        "log_lines": 400,
    },
}


@dataclass
class Artwork:
    artwork_id: str
    restrict: str
    title: str
    user_id: str
    user_name: str
    user_account: str
    type: str
    page_count: int
    is_r18: bool
    tags: list[str]
    image_urls: dict[str, str]
    meta_pages: list[dict[str, Any]]
    raw: dict[str, Any]


class RingLog:
    def __init__(self, max_lines: int = 400):
        self.max_lines = max_lines
        self._lock = threading.Lock()
        self._lines: list[str] = []

    def write(self, message: str) -> None:
        line = f"[{now_iso()}] {message}"
        print(line, flush=True)
        with self._lock:
            self._lines.append(line)
            self._lines = self._lines[-self.max_lines :]

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def artwork_id_from_url(value: str) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    match = re.search(r"(?:artworks/|illust_id=)(\d+)", text)
    return match.group(1) if match else ""


def safe_name(value: str, fallback: str = "pixiv") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "")).strip(" ._")
    return (name or fallback)[:90]


def extension_from_url(url: str) -> str:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    return ".jpg"


def interval_hours(config: dict[str, Any]) -> float:
    if "run_interval_hours" in config:
        try:
            return max(0.01, float(config.get("run_interval_hours") or 12))
        except (TypeError, ValueError):
            return 12.0
    try:
        return max(0.01, float(config.get("run_interval_seconds", 43200)) / 3600)
    except (TypeError, ValueError):
        return 12.0


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return deep_merge(DEFAULT_CONFIG, json.loads(path.read_text(encoding="utf-8-sig")))
        except Exception:
            traceback.print_exc()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def read_refresh_token(config: dict[str, Any], cli_token: str = "") -> str:
    if cli_token.strip():
        return cli_token.strip()
    env_token = str(os.environ.get("PIXIV_REFRESH_TOKEN", "")).strip()
    if env_token:
        return env_token
    token_file = Path(config["refresh_token_file"])
    if not token_file.exists():
        raise FileNotFoundError(f"refresh token file not found: {token_file}")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"refresh token file is empty: {token_file}")
    return token


def write_refresh_token(path: Path, token: str) -> None:
    value = token.strip()
    if not value:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")


def normalize_illust(illust: Any, restrict: str) -> Artwork:
    raw = illust if isinstance(illust, dict) else illust.to_dict()
    tags = []
    for tag in raw.get("tags") or []:
        if isinstance(tag, dict):
            if tag.get("name"):
                tags.append(str(tag["name"]))
            if tag.get("translated_name"):
                tags.append(str(tag["translated_name"]))
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
    return Artwork(
        artwork_id=str(raw.get("id") or ""),
        restrict=restrict,
        title=str(raw.get("title") or ""),
        user_id=str(user.get("id") or ""),
        user_name=str(user.get("name") or ""),
        user_account=str(user.get("account") or ""),
        type=str(raw.get("type") or ""),
        page_count=int(raw.get("page_count") or 0),
        is_r18=is_r18,
        tags=sorted(set(tags)),
        image_urls=raw.get("image_urls") or {},
        meta_pages=raw.get("meta_pages") or [],
        raw=raw,
    )


def original_image_entries(item: Artwork) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    if item.meta_pages:
        for index, page in enumerate(item.meta_pages):
            urls = page.get("image_urls") if isinstance(page, dict) else None
            if isinstance(urls, dict):
                url = urls.get("original")
                if url:
                    entries.append((index, str(url)))
    if not entries:
        meta_single = item.raw.get("meta_single_page") or {}
        url = meta_single.get("original_image_url") or item.image_urls.get("original")
        if url:
            entries.append((0, str(url)))
    return entries


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
    raise RuntimeError("could not resolve pixiv user_id from refresh token")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
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
                    is_r18 integer not null default 0,
                    tags_json text not null default '[]',
                    image_urls_json text not null default '{}',
                    meta_pages_json text not null default '[]',
                    raw_json text not null default '{}',
                    status text not null default 'pending',
                    attempts integer not null default 0,
                    files_json text not null default '[]',
                    error text,
                    first_seen_at text not null,
                    downloaded_at text,
                    updated_at text not null
                );
                create index if not exists idx_artworks_restrict on artworks(restrict_type);
                create index if not exists idx_artworks_r18 on artworks(is_r18);
                create table if not exists runs (
                    id integer primary key autoincrement,
                    started_at text not null,
                    finished_at text,
                    status text not null,
                    discovered integer not null default 0,
                    downloaded integer not null default 0,
                    skipped integer not null default 0,
                    failed integer not null default 0,
                    message text
                );
                """
            )
            self._ensure_columns(
                conn,
                "artworks",
                {
                    "status": "text not null default 'pending'",
                    "attempts": "integer not null default 0",
                    "files_json": "text not null default '[]'",
                    "error": "text",
                    "downloaded_at": "text",
                },
            )
            self._ensure_columns(
                conn,
                "runs",
                {
                    "discovered": "integer not null default 0",
                    "downloaded": "integer not null default 0",
                    "skipped": "integer not null default 0",
                    "failed": "integer not null default 0",
                },
            )
            conn.execute("create index if not exists idx_artworks_status on artworks(status)")

    def _ensure_columns(self, conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"alter table {table} add column {name} {definition}")

    def begin_run(self) -> int:
        with self._lock, self.connect() as conn:
            cur = conn.execute("insert into runs(started_at, status) values(?, 'running')", (now_iso(),))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict[str, int], message: str = "") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                update runs
                set finished_at=?, status=?, discovered=?, downloaded=?, skipped=?, failed=?, message=?
                where id=?
                """,
                (
                    now_iso(),
                    status,
                    stats.get("discovered", 0),
                    stats.get("downloaded", 0),
                    stats.get("skipped", 0),
                    stats.get("failed", 0),
                    message[-2000:],
                    run_id,
                ),
            )

    def upsert_seen(self, item: Artwork) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                insert into artworks(
                    artwork_id, restrict_type, title, user_id, user_name, user_account,
                    type, page_count, is_r18, tags_json, image_urls_json, meta_pages_json,
                    raw_json, first_seen_at, updated_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(artwork_id) do update set
                    restrict_type=excluded.restrict_type,
                    title=excluded.title,
                    user_id=excluded.user_id,
                    user_name=excluded.user_name,
                    user_account=excluded.user_account,
                    type=excluded.type,
                    page_count=excluded.page_count,
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
                    1 if item.is_r18 else 0,
                    json.dumps(item.tags, ensure_ascii=False),
                    json.dumps(item.image_urls, ensure_ascii=False),
                    json.dumps(item.meta_pages, ensure_ascii=False),
                    json.dumps(item.raw, ensure_ascii=False),
                    now_iso(),
                    now_iso(),
                ),
            )

    def get_artwork(self, artwork_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("select * from artworks where artwork_id=?", (artwork_id,)).fetchone()

    def _row_has_files(self, row: sqlite3.Row) -> bool:
        try:
            files = json.loads(row["files_json"] or "[]")
        except json.JSONDecodeError:
            return False
        for file in files:
            try:
                path = Path(file)
                if path.is_file() and path.stat().st_size > 0 and path.suffix.lower() in IMAGE_EXTENSIONS:
                    return True
            except OSError:
                continue
        return False

    def is_done(self, artwork_id: str) -> bool:
        row = self.get_artwork(artwork_id)
        return bool(row and row["status"] == "done" and self._row_has_files(row))

    def should_download(self, artwork_id: str, retry_failed: bool, max_attempts: int) -> bool:
        row = self.get_artwork(artwork_id)
        if not row:
            return True
        if row["status"] == "done":
            return not self._row_has_files(row)
        if row["status"] == "failed":
            if not retry_failed:
                return False
            if max_attempts > 0 and int(row["attempts"]) >= max_attempts:
                return False
        return True

    def mark_result(self, artwork_id: str, status: str, files: list[str], error: str = "") -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                update artworks
                set status=?, attempts=attempts+1, files_json=?, error=?,
                    downloaded_at=case when ?='done' then ? else downloaded_at end,
                    updated_at=?
                where artwork_id=?
                """,
                (
                    status,
                    json.dumps(files, ensure_ascii=False),
                    error[-2000:],
                    status,
                    now_iso(),
                    now_iso(),
                    artwork_id,
                ),
            )

    def recent_artworks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from artworks order by updated_at desc limit ?", (limit,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["files_count"] = len(json.loads(item.get("files_json") or "[]"))
                except json.JSONDecodeError:
                    item["files_count"] = 0
                for key in ("raw_json", "image_urls_json", "meta_pages_json", "tags_json", "files_json"):
                    item.pop(key, None)
                result.append(item)
            return result

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("select * from runs order by id desc limit ?", (limit,))]


class PixivCollector:
    def __init__(self, api: AppPixivAPI, config: dict[str, Any], store: Store, log: RingLog, progress: Any):
        self.api = api
        self.config = config
        self.store = store
        self.log = log
        self.progress = progress

    def fetch_detail(self, artwork_id: str, restrict: str = "manual") -> Artwork:
        result = self.api.illust_detail(artwork_id)
        illust = result.get("illust")
        if not illust:
            raise RuntimeError(f"artwork detail not found: {artwork_id}")
        return normalize_illust(illust, restrict)

    def collect_bookmarks(self, user_id: str) -> list[Artwork]:
        restrict_value = self.config.get("restrict", ["public", "private"])
        restricts = [restrict_value] if isinstance(restrict_value, str) else list(restrict_value)
        restricts = [item for item in restricts if item in {"public", "private"}]
        max_pages = int(self.config.get("max_pages_per_restrict", 0) or 0)
        delay = float(self.config.get("request_delay_seconds", 1.0) or 0)
        stop_after_done = int(self.config.get("stop_after_consecutive_done", 5) or 0)
        stop_id = ""
        stop_marker = self.config.get("stop_marker") or {}
        if stop_marker.get("enabled", True):
            stop_id = artwork_id_from_url(str(stop_marker.get("url") or ""))

        collected: list[Artwork] = []
        for restrict in restricts:
            next_qs: dict[str, Any] | None = None
            page = 0
            consecutive_done = 0
            while True:
                page += 1
                if max_pages > 0 and page > max_pages:
                    self.log.write(f"[{restrict}] reached max_pages={max_pages}")
                    break
                result = self.api.user_bookmarks_illust(**next_qs) if next_qs else self.api.user_bookmarks_illust(
                    user_id=user_id, restrict=restrict
                )
                illusts = list(result.get("illusts") or [])
                if not illusts:
                    self.log.write(f"[{restrict}] empty page={page}; stop")
                    break
                new_on_page = 0
                stop_found = False
                for illust in illusts:
                    item = normalize_illust(illust, restrict)
                    if stop_id and item.artwork_id == stop_id:
                        stop_found = True
                        self.log.write(f"[{restrict}] stop marker found: {stop_id}")
                        break
                    if self.store.is_done(item.artwork_id):
                        consecutive_done += 1
                    else:
                        consecutive_done = 0
                        new_on_page += 1
                    self.store.upsert_seen(item)
                    collected.append(item)
                self.progress(
                    {
                        "phase": "collecting",
                        "collected": len(collected),
                        "restrict": restrict,
                        "page": page,
                        "new_on_last_page": new_on_page,
                    }
                )
                self.log.write(
                    f"[{restrict}] page={page} total={len(collected)} "
                    f"new={new_on_page} consecutive_done={consecutive_done}"
                )
                if stop_found:
                    break
                if stop_after_done > 0 and consecutive_done >= stop_after_done:
                    self.log.write(f"[{restrict}] 连续 {consecutive_done} 个已下载，停止继续翻页")
                    break
                next_url = result.get("next_url")
                if not next_url:
                    break
                next_qs = self.api.parse_qs(next_url)
                if delay > 0:
                    time.sleep(delay)
        return collected


class PixivDownloader:
    def __init__(self, api: AppPixivAPI, config: dict[str, Any], store: Store, log: RingLog):
        self.api = api
        self.config = config
        self.store = store
        self.log = log
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
                "Referer": "https://www.pixiv.net/",
            }
        )

    def artifact_root(self, key: str, default_child: str) -> Path:
        configured = str(self.config.get(key) or "").strip()
        if configured:
            return Path(configured)
        return Path(self.config.get("download_dir", "/downloads")) / default_child

    def artwork_dir(self, item: Artwork) -> Path:
        path = self.artifact_root("image_dir", "images") / item.artwork_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def metadata_dir(self, item: Artwork) -> Path:
        path = self.artifact_root("metadata_dir", "downloads-metadata") / item.artwork_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def fetch_detail(self, item: Artwork) -> Artwork:
        result = self.api.illust_detail(item.artwork_id)
        illust = result.get("illust")
        if not illust:
            raise RuntimeError(f"artwork detail not found: {item.artwork_id}")
        detail = normalize_illust(illust, item.restrict)
        self.store.upsert_seen(detail)
        return detail

    def download_url(self, url: str, target: Path) -> Path:
        tmp = target.with_suffix(target.suffix + ".part")
        with self.session.get(url, timeout=120, stream=True) as response:
            response.raise_for_status()
            with tmp.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    if chunk:
                        file.write(chunk)
        tmp.replace(target)
        if target.stat().st_size <= 0:
            raise RuntimeError(f"downloaded empty file: {target}")
        return target

    def download_images(self, item: Artwork) -> list[str]:
        detail = self.fetch_detail(item)
        entries = original_image_entries(detail)
        if not entries:
            raise RuntimeError(f"no original image URL for artwork {detail.artwork_id}")
        out_dir = self.artwork_dir(detail)
        files = []
        for page_index, url in entries:
            ext = extension_from_url(url)
            target = out_dir / f"{detail.artwork_id}_p{page_index:02d}_{safe_name(detail.title, detail.artwork_id)}{ext}"
            if target.exists() and target.stat().st_size > 0:
                self.log.write(f"exists: {target.name}")
                files.append(str(target))
                continue
            self.download_url(url, target)
            self.log.write(f"image downloaded: {target.name} {target.stat().st_size} bytes")
            files.append(str(target))
        self.write_metadata(detail)
        return files

    def download_ugoira(self, item: Artwork) -> list[str]:
        detail = self.fetch_detail(item)
        out_dir = self.artwork_dir(detail)
        result = self.api.ugoira_metadata(detail.artwork_id)
        metadata = result.get("ugoira_metadata") or {}
        zip_urls = metadata.get("zip_urls") or {}
        zip_url = zip_urls.get("original") or zip_urls.get("medium")
        frames = metadata.get("frames") or []
        if not zip_url:
            raise RuntimeError(f"no ugoira zip URL for artwork {detail.artwork_id}")
        tmp_dir = out_dir / "_tmp_ugoira"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_path = tmp_dir / f"{detail.artwork_id}.zip"
        self.download_url(zip_url, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp_dir)
        target = out_dir / f"{detail.artwork_id}_ugoira_{safe_name(detail.title, detail.artwork_id)}.gif"
        self.convert_ugoira_to_gif(tmp_dir, frames, target)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        self.write_metadata(detail, {"ugoira_metadata": metadata})
        self.log.write(f"ugoira gif saved: {target.name} {target.stat().st_size} bytes")
        return [str(target)]

    def convert_ugoira_to_gif(self, tmp_dir: Path, frames: list[dict[str, Any]], target: Path) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found; cannot convert ugoira to gif")
        frame_paths = []
        if frames:
            for frame in frames:
                file_name = str(frame.get("file") or "")
                if file_name:
                    frame_paths.append((tmp_dir / file_name, int(frame.get("delay") or 100)))
        if not frame_paths:
            images = sorted([p for p in tmp_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}])
            fallback_delay = int(1000 / max(1, int(self.config.get("media", {}).get("ugoira_fps_fallback", 12))))
            frame_paths = [(path, fallback_delay) for path in images]
        if not frame_paths:
            raise RuntimeError("ugoira zip did not contain image frames")
        concat = tmp_dir / "frames.txt"
        with concat.open("w", encoding="utf-8") as file:
            for frame_path, delay_ms in frame_paths:
                file.write(f"file '{frame_path.as_posix()}'\n")
                file.write(f"duration {max(1, delay_ms) / 1000:.3f}\n")
            file.write(f"file '{frame_paths[-1][0].as_posix()}'\n")
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-vf",
            "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-loop",
            "0",
            str(target),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if completed.returncode != 0 or not target.exists() or target.stat().st_size <= 0:
            raise RuntimeError((completed.stderr or completed.stdout)[-2000:])

    def write_metadata(self, item: Artwork, extra: dict[str, Any] | None = None) -> None:
        out_dir = self.metadata_dir(item)
        data = dict(item.raw)
        if extra:
            data.update(extra)
        (out_dir / f"{item.artwork_id}.info.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def download_item(self, item: Artwork, force: bool = False) -> tuple[str, list[str], str]:
        self.store.upsert_seen(item)
        retry_failed = bool(self.config.get("retry_failed", True))
        max_attempts = int(self.config.get("max_download_attempts", 0) or 0)
        if not force and not self.store.should_download(item.artwork_id, retry_failed, max_attempts):
            return "skipped", [], ""
        try:
            if item.type == "ugoira":
                files = self.download_ugoira(item)
            else:
                files = self.download_images(item)
            self.store.mark_result(item.artwork_id, "done", files, "")
            return "done", files, ""
        except Exception as error:
            self.store.mark_result(item.artwork_id, "failed", [], str(error))
            return "failed", [], str(error)


class App:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.log = RingLog(int(self.config.get("web", {}).get("log_lines", 400)))
        self.store = Store(Path(self.config["database"]))
        self.run_lock = threading.Lock()
        self.running = False
        self.stop_event = threading.Event()
        self.next_run_at = 0.0
        self.last_run_message = ""
        self.progress_lock = threading.Lock()
        self.progress = self.empty_progress()

    def empty_progress(self) -> dict[str, Any]:
        return {
            "phase": "idle",
            "collected": 0,
            "download_total": 0,
            "download_done": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "current": "",
            "restrict": "",
            "page": 0,
            "new_on_last_page": 0,
        }

    def set_progress(self, patch: dict[str, Any]) -> None:
        with self.progress_lock:
            self.progress.update(patch)

    def get_progress(self) -> dict[str, Any]:
        with self.progress_lock:
            return dict(self.progress)

    def reload_config(self) -> None:
        self.config = load_config(self.config_path)
        self.log.max_lines = int(self.config.get("web", {}).get("log_lines", 400))

    def save_config(self, patch: dict[str, Any]) -> None:
        self.config = deep_merge(self.config, patch)
        save_config(self.config_path, self.config)

    def token_present(self) -> bool:
        token_file = Path(self.config["refresh_token_file"])
        return token_file.exists() and token_file.stat().st_size > 0

    def make_api(self) -> tuple[AppPixivAPI, str]:
        token = read_refresh_token(self.config)
        api = auth_api(token)
        user_id = get_own_user_id(api)
        return api, user_id

    def test_token(self) -> None:
        if not self.run_lock.acquire(blocking=False):
            self.log.write("已有任务正在运行，本次 Token 测试未启动")
            return
        self.running = True
        message = ""
        try:
            self.reload_config()
            self.set_progress({"phase": "testing"})
            api, user_id = self.make_api()
            result = api.user_detail(user_id)
            user = result.get("user") or {}
            self.log.write(f"Token 测试成功：user_id={user_id} name={user.get('name') or '-'}")
            message = "ok"
        except Exception as error:
            message = str(error)
            self.log.write(f"Token 测试失败：{message}")
            self.log.write(traceback.format_exc())
        finally:
            self.set_progress({"phase": "idle"})
            self.last_run_message = message
            self.running = False
            self.run_lock.release()

    def run_once(self) -> dict[str, int]:
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("a run is already active")
        self.running = True
        with self.progress_lock:
            self.progress = self.empty_progress()
            self.progress["phase"] = "starting"
        run_id = self.store.begin_run()
        stats = {"discovered": 0, "downloaded": 0, "skipped": 0, "failed": 0}
        message = ""
        try:
            self.reload_config()
            api, user_id = self.make_api()
            self.log.write(f"Run started for Pixiv user_id={user_id}")
            collector = PixivCollector(api, self.config, self.store, self.log, self.set_progress)
            artworks = collector.collect_bookmarks(user_id)
            stats["discovered"] = len(artworks)
            self.set_progress({"phase": "downloading", "download_total": len(artworks), "download_done": 0})
            downloader = PixivDownloader(api, self.config, self.store, self.log)
            delay = float(self.config.get("download_delay_seconds", 1.0) or 0)
            for index, item in enumerate(artworks, start=1):
                self.set_progress({"phase": "downloading", "current": f"{item.artwork_id} {item.title}"})
                status, files, error = downloader.download_item(item)
                if status == "done":
                    stats["downloaded"] += 1
                elif status == "skipped":
                    stats["skipped"] += 1
                else:
                    stats["failed"] += 1
                    self.log.write(f"Failed {item.artwork_id}: {error}")
                self.set_progress(
                    {
                        "download_done": index,
                        "downloaded": stats["downloaded"],
                        "skipped": stats["skipped"],
                        "failed": stats["failed"],
                    }
                )
                self.log.write(f"Progress {index}/{len(artworks)}: {status} {item.artwork_id}")
                if delay > 0 and index < len(artworks):
                    time.sleep(delay + random.random())
            message = "ok"
            self.set_progress({"phase": "finished", "current": ""})
            self.store.finish_run(run_id, "done", stats, message)
            self.log.write(f"Run finished: {stats}")
            return stats
        except Exception as error:
            message = str(error)
            self.set_progress({"phase": "failed", "current": ""})
            self.store.finish_run(run_id, "failed", stats, message)
            self.log.write(f"Run failed: {message}")
            self.log.write(traceback.format_exc())
            raise
        finally:
            self.last_run_message = message
            self.running = False
            self.run_lock.release()

    def manual_download(self, url: str) -> None:
        if not self.run_lock.acquire(blocking=False):
            self.log.write("已有任务正在运行，本次手动下载未启动")
            return
        self.running = True
        message = ""
        run_id = self.store.begin_run()
        stats = {"discovered": 1, "downloaded": 0, "skipped": 0, "failed": 0}
        try:
            self.reload_config()
            artwork_id = artwork_id_from_url(url)
            if not artwork_id:
                raise RuntimeError("请输入有效的 Pixiv 作品链接或作品 ID")
            api, _user_id = self.make_api()
            collector = PixivCollector(api, self.config, self.store, self.log, self.set_progress)
            item = collector.fetch_detail(artwork_id)
            self.set_progress({"phase": "manual", "download_total": 1, "download_done": 0, "current": item.title})
            downloader = PixivDownloader(api, self.config, self.store, self.log)
            status, files, error = downloader.download_item(item, force=True)
            if status == "done":
                stats["downloaded"] = 1
            else:
                stats["failed"] = 1
                self.log.write(f"手动下载失败 {artwork_id}: {error}")
            message = status if not error else error
            self.set_progress({"phase": "finished", "download_done": 1, "current": ""})
            self.store.finish_run(run_id, "done" if status == "done" else "failed", stats, message)
        except Exception as error:
            message = str(error)
            stats["failed"] = 1
            self.log.write(f"手动下载异常：{message}")
            self.log.write(traceback.format_exc())
            self.store.finish_run(run_id, "failed", stats, message)
        finally:
            self.last_run_message = message
            self.running = False
            self.run_lock.release()

    def start_run_thread(self) -> None:
        threading.Thread(target=lambda: self._thread_wrap(self.run_once), daemon=True).start()

    def start_manual_thread(self, url: str) -> None:
        threading.Thread(target=lambda: self.manual_download(url), daemon=True).start()

    def start_token_test_thread(self) -> None:
        threading.Thread(target=self.test_token, daemon=True).start()

    def _thread_wrap(self, fn: Any) -> None:
        try:
            fn()
        except Exception:
            pass

    def scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            self.reload_config()
            interval = int(interval_hours(self.config) * 3600)
            if self.next_run_at <= 0:
                self.next_run_at = time.time() + 5
            if time.time() >= self.next_run_at and not self.running:
                self.start_run_thread()
                self.next_run_at = time.time() + interval
            self.stop_event.wait(5)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "next_run_at": datetime.fromtimestamp(self.next_run_at).isoformat() if self.next_run_at else "",
            "token_present": self.token_present(),
            "config": self.config,
            "progress": self.get_progress(),
            "runs": self.store.recent_runs(),
            "artworks": self.store.recent_artworks(),
            "logs": self.log.lines(),
            "last_run_message": self.last_run_message,
            "run_interval_hours": interval_hours(self.config),
        }


def html_page(app: App) -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pixiv Auto Downloader</title>
  <style>
    :root { color-scheme: light; --bg:#f6f7f9; --panel:#fff; --line:#d9dde5; --text:#1d2433; --muted:#657084; --accent:#111827; }
    body { margin:0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:var(--bg); color:var(--text); }
    header { min-height:56px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:0 24px; background:#111827; color:white; }
    main { max-width:1180px; margin:0 auto; padding:20px; display:grid; gap:16px; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }
    h1 { font-size:18px; margin:0; } h2 { font-size:16px; margin:0 0 12px; }
    label { display:block; color:var(--muted); font-size:13px; margin:10px 0 5px; }
    input, textarea { width:100%; box-sizing:border-box; border:1px solid var(--line); border-radius:6px; padding:9px 10px; font:inherit; background:white; }
    textarea { min-height:92px; resize:vertical; }
    button { border:0; background:var(--accent); color:white; border-radius:6px; padding:9px 14px; cursor:pointer; }
    button.secondary { background:#475569; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
    .pill { display:inline-flex; align-items:center; padding:3px 8px; border-radius:999px; background:#e6edf6; font-size:12px; color:#334155; }
    table { width:100%; border-collapse:collapse; font-size:13px; } th,td { border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top; }
    pre { margin:0; background:#0f172a; color:#dbeafe; padding:12px; border-radius:6px; overflow:auto; max-height:520px; white-space:pre-wrap; }
    progress { width:100%; height:16px; accent-color:#111827; }
    .progress-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:12px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfcfe; }
    .metric strong { display:block; font-size:18px; margin-top:4px; }
    .help { color:var(--muted); font-size:13px; line-height:1.65; }
    .muted { color:var(--muted); } .status { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    @media (max-width:760px) { .grid,.progress-grid { grid-template-columns:1fr; } header { padding:10px 14px; align-items:flex-start; flex-direction:column; } main { padding:12px; } }
  </style>
</head>
<body>
  <header><h1>Pixiv Auto Downloader</h1><div class="status"><span id="runningPill" class="pill">运行状态：读取中</span><span id="tokenPill" class="pill">Token：读取中</span></div></header>
  <main>
    <section>
      <h2>控制</h2>
      <div class="muted" id="scheduleText">正在读取状态...</div>
      <div class="actions">
        <form method="post" action="/run"><button type="submit">立即运行</button></form>
        <form method="post" action="/reload"><button class="secondary" type="submit">重新读取配置</button></form>
      </div>
    </section>
    <section>
      <h2>运行进度</h2>
      <div class="muted" id="phaseText">等待中</div>
      <label>下载总进度</label>
      <progress id="totalProgress" value="0" max="1"></progress>
      <div class="progress-grid">
        <div class="metric">已采集作品<strong id="collectedMetric">0</strong></div>
        <div class="metric">已处理/总数<strong id="downloadMetric">0 / 0</strong></div>
        <div class="metric">已下载<strong id="doneMetric">0</strong></div>
        <div class="metric">失败<strong id="failedMetric">0</strong></div>
      </div>
      <div class="muted" id="currentText"></div>
    </section>
    <section>
      <h2>手动单条下载</h2>
      <form method="post" action="/manual-download">
        <label>Pixiv 作品 URL 或作品 ID</label>
        <input name="manual_url" placeholder="https://www.pixiv.net/artworks/123456789">
        <div class="actions"><button type="submit">下载这一条</button></div>
      </form>
    </section>
    <section>
      <h2>配置</h2>
      <form method="post" action="/settings">
        <div class="grid">
          <div><label>运行间隔（小时）</label><input id="intervalHoursInput" name="interval_hours" type="number" min="0.1" step="0.1"></div>
          <div><label>每类收藏最大页数（0 表示不限）</label><input id="maxPagesInput" name="max_pages" type="number" min="0" step="1"></div>
          <div><label>连续已下载停止数</label><input id="stopDoneInput" name="stop_done" type="number" min="0" step="1"></div>
          <div><label>停止标记 URL</label><input id="stopUrlInput" name="stop_url"></div>
        </div>
        <div class="actions"><button type="submit">保存配置</button></div>
      </form>
    </section>
    <section>
      <h2>Refresh Token</h2>
      <div class="help">
        获取方式：电脑执行 <code>gallery-dl oauth:pixiv</code>，复制命令行给出的登录链接，用浏览器打开；按 F12 打开开发者工具并切到 Network；登录 Pixiv；找到最后一个 <code>callback?state=...</code> 请求，复制 URL 里的 <code>code</code> 参数；回到命令行粘贴 code。成功后命令行会显示 <code>Your 'refresh-token' is</code>，把下一行粘贴到这里。code 大约 30 秒过期。
      </div>
      <form method="post" action="/token">
        <label>粘贴 refresh-token</label>
        <textarea name="refresh_token"></textarea>
        <div class="actions"><button type="submit">保存 Token</button></div>
      </form>
      <form method="post" action="/token-test"><div class="actions"><button class="secondary" type="submit">测试 Token</button></div></form>
    </section>
    <section>
      <h2>最近运行</h2>
      <table><thead><tr><th>ID</th><th>开始</th><th>状态</th><th>发现</th><th>下载</th><th>跳过</th><th>失败</th></tr></thead><tbody id="runsBody"></tbody></table>
    </section>
    <section>
      <h2>下载记录</h2>
      <table><thead><tr><th>作品</th><th>标题</th><th>类型</th><th>R-18</th><th>状态</th><th>文件</th><th>错误</th></tr></thead><tbody id="artworksBody"></tbody></table>
    </section>
    <section>
      <h2>日志</h2>
      <pre id="logBox"></pre>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    let filledForm = false;
    function phaseName(phase) {
      return {idle:"空闲", starting:"准备运行", collecting:"正在采集收藏", downloading:"正在下载", manual:"手动下载", testing:"正在测试 Token", finished:"已完成", failed:"运行失败"}[phase] || phase || "未知";
    }
    function updateProgress(progress) {
      const total = Number(progress.download_total || 0);
      const done = Number(progress.download_done || 0);
      $("phaseText").textContent = `阶段：${phaseName(progress.phase)}；收藏：${progress.restrict || "-"}；页数：${progress.page || 0}；本页新增：${progress.new_on_last_page || 0}`;
      $("totalProgress").max = total > 0 ? total : 1;
      $("totalProgress").value = total > 0 ? done : 0;
      $("collectedMetric").textContent = progress.collected || 0;
      $("downloadMetric").textContent = `${done} / ${total}`;
      $("doneMetric").textContent = progress.downloaded || 0;
      $("failedMetric").textContent = progress.failed || 0;
      $("currentText").textContent = progress.current ? `当前：${progress.current}` : "";
    }
    function updateTables(data) {
      $("runsBody").innerHTML = (data.runs || []).map((r) =>
        `<tr><td>${r.id}</td><td>${esc(r.started_at)}</td><td>${esc(r.status)}</td><td>${r.discovered}</td><td>${r.downloaded}</td><td>${r.skipped}</td><td>${r.failed}</td></tr>`
      ).join("");
      $("artworksBody").innerHTML = (data.artworks || []).map((a) =>
        `<tr><td><a href="https://www.pixiv.net/artworks/${esc(a.artwork_id)}" target="_blank">${esc(a.artwork_id)}</a></td><td>${esc(a.title)}</td><td>${esc(a.type)}</td><td>${a.is_r18 ? "是" : "否"}</td><td>${esc(a.status)}</td><td>${a.files_count || 0}</td><td>${esc((a.error || "").slice(0, 120))}</td></tr>`
      ).join("");
    }
    function fillFormOnce(data) {
      if (filledForm) return;
      const cfg = data.config || {};
      $("intervalHoursInput").value = data.run_interval_hours || cfg.run_interval_hours || 12;
      $("maxPagesInput").value = cfg.max_pages_per_restrict || 0;
      $("stopDoneInput").value = cfg.stop_after_consecutive_done || 5;
      $("stopUrlInput").value = cfg.stop_marker?.url || "";
      filledForm = true;
    }
    async function refreshStatus() {
      try {
        const res = await fetch("/api/status", {cache: "no-store"});
        const data = await res.json();
        $("runningPill").textContent = `运行状态：${data.running ? "运行中" : "空闲"}`;
        $("tokenPill").textContent = `Token：${data.token_present ? "已保存" : "未保存"}`;
        $("scheduleText").textContent = `下一次自动运行：${data.next_run_at || "未排程"}；周期：${data.run_interval_hours || 12} 小时`;
        updateProgress(data.progress || {});
        updateTables(data);
        fillFormOnce(data);
        const logBox = $("logBox");
        const shouldStick = Math.abs(logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight) < 40;
        logBox.textContent = (data.logs || []).join("\\n");
        if (shouldStick) logBox.scrollTop = logBox.scrollHeight;
      } catch (error) {
        $("scheduleText").textContent = `状态刷新失败：${error}`;
      }
    }
    refreshStatus();
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>"""


def redirect(handler: BaseHTTPRequestHandler, location: str = "/") -> None:
    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", location)
    handler.end_headers()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/api/status"):
                body = json.dumps(app.status(), ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = html_page(app).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            if self.path == "/run":
                app.start_run_thread()
                redirect(self)
                return
            if self.path == "/manual-download":
                app.start_manual_thread((form.get("manual_url") or [""])[0])
                redirect(self)
                return
            if self.path == "/reload":
                app.reload_config()
                redirect(self)
                return
            if self.path == "/token":
                token = (form.get("refresh_token") or [""])[0]
                write_refresh_token(Path(app.config["refresh_token_file"]), token)
                app.log.write("已从网页端保存 refresh-token")
                redirect(self)
                return
            if self.path == "/token-test":
                app.start_token_test_thread()
                redirect(self)
                return
            if self.path == "/settings":
                try:
                    hours = max(0.1, float((form.get("interval_hours") or ["12"])[0] or "12"))
                except ValueError:
                    hours = 12.0
                patch = {
                    "run_interval_hours": hours,
                    "run_interval_seconds": int(hours * 3600),
                    "max_pages_per_restrict": int((form.get("max_pages") or ["0"])[0] or 0),
                    "stop_after_consecutive_done": int((form.get("stop_done") or ["5"])[0] or 5),
                    "stop_marker": {"url": (form.get("stop_url") or [""])[0]},
                }
                app.save_config(patch)
                app.log.write("已从网页端保存设置")
                redirect(self)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def copy_example_config(config_path: Path) -> None:
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--run-once", action="store_true")
    args = parser.parse_args()
    config_path = Path(args.config)
    copy_example_config(config_path)
    app = App(config_path)
    if args.run_once:
        app.run_once()
        return 0
    scheduler = threading.Thread(target=app.scheduler_loop, daemon=True)
    scheduler.start()
    web_cfg = app.config.get("web", {})
    host = str(web_cfg.get("host", "0.0.0.0"))
    port = int(web_cfg.get("port", 8080))
    app.log.write(f"Web UI listening on {host}:{port}")
    server = ThreadingHTTPServer((host, port), make_handler(app))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.stop_event.set()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
