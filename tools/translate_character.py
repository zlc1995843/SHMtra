#!/usr/bin/env python3
"""Download and translate one Sweet Home Maid character's card stories.

The tool reads the live AdvStory Cocos bundle catalog, downloads only the
matching TextAsset JSON files, translates visible story lines through the
DeepSeek OpenAI-compatible API, and writes launcher-ready files under the
repository's original assets/AdvStory/import path.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_GAME_ROOT = Path(r"G:\04 DMM\sweet-home-maid")
DEFAULT_CDN_ROOT = "https://game.sweet-home-maid.com/r/7LCHDxB8msHV"
DEFAULT_MODEL = "deepseek-v4-pro"
API_URL = "https://api.deepseek.com/chat/completions"
JAPANESE_RE = re.compile(r"[\u3040-\u30ff]")
FULL_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

SYSTEM_PROMPT = """你是专业的日中游戏本地化译者。把《Sweet Home Maid》的日文剧情翻译成自然、流畅的简体中文。

硬性要求：
1. 只返回合法 JSON 对象，格式为 {"translations":[{"id":"原ID","zh":"中文"}]}。
2. 不增删条目，不修改 id，不附加解释。
3. 保留原文的语气、停顿、波浪号、感叹号、暧昧或成人向表达，不审查、不弱化。
4. 严格遵守随请求附带的译名词库，较长的称呼优先于基础名字。
5. 输出文本中不要残留日文假名。专有名词没有固定译名时采用自然音译。
6. 保留输入字符串中的换行；可为中文可读性微调分行，但单行不超过 25 个全角字符。
7. 结合相邻句理解承接、省略、共同宾语和人物指代。CG 旁白通常是男性主人公第一人称，不得擅自改成女性或第三人称。
8. 不得删减、概括或净化成人内容，不得增加原文不存在的信息。
"""


@dataclass(frozen=True)
class StoryAsset:
    logical_name: str
    uuid: str
    version: str

    @property
    def relative_path(self) -> Path:
        return Path("assets") / "AdvStory" / "import" / self.uuid[:2] / (
            f"{self.uuid}.{self.version}.json"
        )


@dataclass(frozen=True)
class Segment:
    segment_id: str
    story_name: str
    indices: tuple[int, ...]
    source: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def decompress_uuid(value: str) -> str:
    value = value.strip()
    if FULL_UUID_RE.fullmatch(value):
        return value.lower()
    if len(value) != 22 or not re.fullmatch(r"[A-Za-z0-9+/]{22}", value):
        raise ValueError(f"Unrecognized Cocos UUID: {value}")
    raw = value[:2].lower() + base64.b64decode(value[2:] + "==").hex()
    if len(raw) != 32:
        raise ValueError(f"Invalid Cocos UUID length: {value}")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def newest_advstory_config(game_root: Path) -> Path:
    candidates = sorted(
        (game_root / "assets" / "AdvStory").glob("config.*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("AdvStory config.*.json was not found")
    return candidates[0]


def load_catalog_by_prefixes(
    config_path: Path,
    prefixes: tuple[str, ...],
    exclude_prefixes: tuple[str, ...] = (),
) -> list[StoryAsset]:
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    uuids = config.get("uuids", [])
    raw_versions = config.get("versions", {}).get("import", [])
    versions = {
        int(raw_versions[index]): str(raw_versions[index + 1])
        for index in range(0, len(raw_versions) - 1, 2)
    }
    result: list[StoryAsset] = []
    for raw_index, path_info in config.get("paths", {}).items():
        if not isinstance(path_info, list) or not path_info:
            continue
        logical_name = str(path_info[0])
        if not logical_name.startswith(prefixes):
            continue
        if exclude_prefixes and logical_name.startswith(exclude_prefixes):
            continue
        index = int(raw_index)
        if index not in versions or not 0 <= index < len(uuids):
            raise ValueError(f"Missing UUID/version for {logical_name}")
        result.append(
            StoryAsset(logical_name, decompress_uuid(str(uuids[index])), versions[index])
        )
    result.sort(key=lambda item: item.logical_name)
    if not result:
        raise ValueError(f"No stories found for prefixes: {', '.join(prefixes)}")
    return result


def load_story_catalog(config_path: Path, character_id: int) -> list[StoryAsset]:
    return load_catalog_by_prefixes(config_path, (f"Card/story{character_id:03d}",))


def request_bytes(url: str, retries: int = 10, timeout: int = 45) -> bytes:
    headers = {
        "Accept": "application/json,*/*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SHMtra/1.0",
    }
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=timeout
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                raise
            if attempt >= retries:
                raise
        except (OSError, TimeoutError, urllib.error.URLError):
            if attempt >= retries:
                raise
        time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))) + random.random() * 0.25)
    raise RuntimeError("unreachable")


def download_story(
    asset: StoryAsset,
    source_root: Path,
    game_root: Path,
    cdn_root: str,
) -> tuple[StoryAsset, Path, bool]:
    destination = source_root / asset.relative_path
    if destination.is_file():
        load_text_asset(destination.read_bytes(), asset.logical_name)
        return asset, destination, False

    local_copy = game_root / asset.relative_path
    if local_copy.is_file():
        payload = local_copy.read_bytes()
    else:
        url = f"{cdn_root.rstrip('/')}/{asset.relative_path.as_posix()}"
        payload = request_bytes(url)
    load_text_asset(payload, asset.logical_name)
    atomic_write(destination, payload)
    return asset, destination, True


def load_text_asset(payload: bytes, logical_name: str) -> tuple[Any, str]:
    document = json.loads(payload.decode("utf-8-sig"))
    try:
        record = document[5][0]
        asset_name = str(record[1])
        script = record[2]
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError(f"Unexpected TextAsset structure: {logical_name}") from exc
    if not isinstance(script, str):
        raise ValueError(f"TextAsset text is not a string: {logical_name}")
    expected_name = logical_name.rsplit("/", 1)[-1]
    if asset_name != expected_name:
        raise ValueError(
            f"TextAsset name mismatch for {logical_name}: got {asset_name!r}"
        )
    return document, script


def is_translatable_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith(("@", "//", "$$", "#", ";")):
        return False
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", stripped))


def localize_speaker_labels(script: str, glossary: list[dict[str, str]]) -> str:
    """Translate visible labels of ShowCastMessage and ShowMessage directives.

    The second field is the engine's internal cast key and must stay Japanese.
    The optional third field is the label rendered in the message box.  When a
    known cast uses the short two-field form, append a translated display label
    without changing the internal key.
    """
    replacements = sorted(
        (
            (str(item.get("source", "")), str(item.get("target", "")))
            for item in glossary
            if str(item.get("source", "")) and str(item.get("target", ""))
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    exact = dict(replacements)
    # Engine-only cast variants belong in display labels, not in the prose
    # glossary: enforcing them in dialogue can reject natural translations.
    exact.update({"ニアP1": "妮娅", "全員": "众人"})
    pattern = re.compile(
        r"^(?P<prefix>[ \t]*@(?:ShowCastMessage|ShowMessage),)(?P<cast>[^,\r\n]*)"
        r"(?:,(?P<label>[^\r\n]*))?(?P<ending>\r?)$",
        flags=re.MULTILINE,
    )

    def replace_label(value: str) -> str:
        result = value
        for source, target in replacements:
            # B/P-number suffixes select an engine cast variant and must not be
            # shown to players as part of the localized character name.
            result = re.sub(
                re.escape(source) + r"(?:B|P\d+)", target, result
            )
            result = result.replace(source, target)
        return result

    def rewrite(match: re.Match[str]) -> str:
        cast = match.group("cast")
        label = match.group("label")
        if label is not None:
            localized = replace_label(label)
            return match.group("prefix") + cast + "," + localized + match.group("ending")
        localized = replace_label(cast)
        if localized == cast:
            return match.group(0)
        return match.group("prefix") + cast + "," + localized + match.group("ending")

    return pattern.sub(rewrite, script)


def limit_visible_text_lines(script: str, maximum_lines: int = 2) -> str:
    """Reflow each visible message block so the game never renders three lines."""
    if maximum_lines != 2:
        raise ValueError("Only the two-line dialogue layout is supported")
    lines = script.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[str] = []
    index = 0
    break_after = set("，。！？；：、…）】》」』~～!?,.;:")
    while index < len(lines):
        if not is_translatable_line(lines[index]):
            result.append(lines[index])
            index += 1
            continue
        end = index
        block: list[str] = []
        while end < len(lines) and is_translatable_line(lines[end]):
            block.append(lines[end].strip())
            end += 1
        if len(block) <= maximum_lines:
            result.extend(lines[index:end])
            index = end
            continue
        text = "".join(block)
        midpoint = len(text) // 2
        candidates = [
            position + 1
            for position, character in enumerate(text[:-1])
            if character in break_after
        ]
        split_at = min(candidates, key=lambda value: abs(value - midpoint)) if candidates else midpoint
        # Avoid an extremely short line when punctuation happens only near an edge.
        if split_at < len(text) // 3 or split_at > (len(text) * 2) // 3:
            split_at = midpoint
        result.extend((text[:split_at], text[split_at:]))
        index = end
    return "\r\n".join(result)


def extract_segments(story_name: str, script: str) -> tuple[list[str], list[Segment]]:
    lines = script.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[Segment] = []
    current_indices: list[int] = []

    def flush() -> None:
        if not current_indices:
            return
        number = len(segments) + 1
        source = "\n".join(lines[index] for index in current_indices)
        segments.append(
            Segment(
                f"{story_name}:{number:04d}",
                story_name,
                tuple(current_indices),
                source,
            )
        )
        current_indices.clear()

    for index, line in enumerate(lines):
        if is_translatable_line(line):
            current_indices.append(index)
        else:
            flush()
    flush()
    return lines, segments


def batches_by_characters(
    segments: Iterable[Segment], maximum: int, maximum_segments: int = 80
) -> list[list[Segment]]:
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        addition = len(segment.source) + len(segment.segment_id) + 80
        if current and (size + addition > maximum or len(current) >= maximum_segments):
            batches.append(current)
            current = []
            size = 0
        current.append(segment)
        size += addition
    if current:
        batches.append(current)
    return batches


def protected_markers(value: str) -> list[str]:
    return re.findall(r"\{\{[^{}]+\}\}|\$\$[^\s]+|\[[^\]\r\n]+\]", value)


def load_glossary(path: Path) -> tuple[list[dict[str, str]], str]:
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_terms = document.get("terms", [])
    terms: list[dict[str, str]] = []
    for item in raw_terms:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip()
        target = str(item.get("target", "")).strip()
        if source and target:
            terms.append({"source": source, "target": target})
    terms.sort(key=lambda item: len(item["source"]), reverse=True)
    fingerprint = sha256_bytes(
        json.dumps(terms, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return terms, fingerprint


def required_glossary_targets(
    source: str, glossary: list[dict[str, str]]
) -> list[tuple[str, str]]:
    occupied = [False] * len(source)
    required: list[tuple[str, str]] = []
    for item in glossary:
        term = item["source"]
        # These generic speaker labels have several equally natural prose
        # translations. Keep them available for display-name localization but
        # do not force one wording inside dialogue and narration.
        if term in {"みんな", "住人"}:
            continue
        start = 0
        matched = False
        while True:
            index = source.find(term, start)
            if index < 0:
                break
            end = index + len(term)
            # 「ロイズちゃっ……」 is a deliberately interrupted form of
            # 「ロイズちゃん」. Preserve the stammer naturally instead of
            # forcing the unshortened base-name target into that line.
            if term == "ロイズ" and source[end : end + 2] == "ちゃ":
                start = index + 1
                continue
            # 「ニア」 also occurs inside unrelated katakana words such as
            # 「マニアック」 and 「アンモニア」. Only treat it as the character
            # name when it is not joined to another katakana character.
            if term == "ニア":
                previous = source[index - 1] if index > 0 else ""
                following = source[end] if end < len(source) else ""
                if re.fullmatch(r"[ァ-ヺー]", previous) or re.fullmatch(
                    r"[ァ-ヺー]", following
                ):
                    start = index + 1
                    continue
            if not any(occupied[index:end]):
                for position in range(index, end):
                    occupied[position] = True
                matched = True
            start = index + 1
        if matched:
            required.append((term, item["target"]))
    return required


def validate_translation(
    segment: Segment,
    translated: str,
    glossary: list[dict[str, str]],
) -> None:
    if not translated.strip():
        raise ValueError(f"Empty translation: {segment.segment_id}")
    if protected_markers(segment.source) != protected_markers(translated):
        raise ValueError(f"Protected marker mismatch: {segment.segment_id}")
    if JAPANESE_RE.search(translated):
        raise ValueError(f"Japanese kana remains: {segment.segment_id}")
    missing_terms = [
        f"{source}->{target}"
        for source, target in required_glossary_targets(segment.source, glossary)
        if target not in translated
    ]
    if missing_terms:
        raise ValueError(
            f"Glossary mismatch: {segment.segment_id}: {', '.join(missing_terms)}"
        )


def api_request(
    api_key: str,
    model: str,
    batch: list[Segment],
    glossary: list[dict[str, str]],
) -> dict[str, str]:
    user_payload = {
        "task": "translate_game_scenario_segments_to_simplified_chinese_json",
        "segments": [
            {"id": segment.segment_id, "ja": segment.source} for segment in batch
        ],
        "glossary": glossary,
    }
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                response_document = json.loads(response.read().decode("utf-8-sig"))
            content = response_document["choices"][0]["message"]["content"]
            translated_document = json.loads(content)
            items = translated_document.get("translations", [])
            result = {
                str(item["id"]): str(item["zh"])
                for item in items
                if isinstance(item, dict) and "id" in item and "zh" in item
            }
            expected = {segment.segment_id for segment in batch}
            if set(result) != expected:
                missing = sorted(expected.difference(result))[:8]
                extra = sorted(set(result).difference(expected))[:8]
                raise ValueError(f"DeepSeek ID mismatch; missing={missing}, extra={extra}")
            for segment in batch:
                validate_translation(segment, result[segment.segment_id], glossary)
            return result
        except urllib.error.HTTPError as exc:
            last_error = exc
            if 400 <= exc.code < 500 and exc.code not in {405, 429}:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
                raise RuntimeError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        except (OSError, TimeoutError, urllib.error.URLError, ValueError, KeyError) as exc:
            last_error = exc
        if attempt < 3:
            time.sleep(min(20.0, 1.5 * (2 ** (attempt - 1))) + random.random())
    raise RuntimeError(f"DeepSeek request failed after 3 retries: {last_error}")


class TranslationCache:
    def __init__(self, path: Path, model: str, glossary_fingerprint: str) -> None:
        self.path = path
        self.model = model
        self.glossary_fingerprint = glossary_fingerprint
        self.lock = threading.Lock()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        self.entries: dict[str, dict[str, str]] = raw.get("entries", {})

    @staticmethod
    def key(segment: Segment, model: str, glossary_fingerprint: str) -> str:
        material = (
            f"{model}\0{glossary_fingerprint}\0{segment.segment_id}\0{segment.source}"
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def get(self, segment: Segment) -> str | None:
        entry = self.entries.get(
            self.key(segment, self.model, self.glossary_fingerprint)
        )
        if not entry or entry.get("source") != segment.source:
            return None
        return entry.get("translation")

    def update(self, segment: Segment, translation: str) -> None:
        with self.lock:
            self.entries[
                self.key(segment, self.model, self.glossary_fingerprint)
            ] = {
                "id": segment.segment_id,
                "source": segment.source,
                "translation": translation,
                "model": self.model,
                "glossary_fingerprint": self.glossary_fingerprint,
            }

    def save(self) -> None:
        with self.lock:
            payload = json.dumps(
                {"version": 1, "entries": self.entries},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8") + b"\n"
            atomic_write(self.path, payload)


def unchanged_directives(source: str, translated: str) -> bool:
    def directives(value: str) -> list[str]:
        return [
            line
            for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if line.lstrip().startswith(("@", "$$"))
        ]

    return directives(source) == directives(translated)


def reflow_existing_translations(repo_root: Path) -> tuple[int, int]:
    """Apply the two-line layout to every file recorded by translation manifests."""
    glossary, _ = load_glossary(repo_root / "Lang/CHS/translation-glossary.json")
    changed_files = 0
    changed_blocks = 0
    for manifest_path in sorted((repo_root / "translations").glob("*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest_changed = False
        for item in manifest.get("files", []):
            relative = Path(str(item["path"]))
            output_path = repo_root / relative
            payload = output_path.read_bytes()
            document, script = load_text_asset(payload, str(item["story"]))
            _, before = extract_segments(str(item["story"]), script)
            oversized = sum(1 for segment in before if len(segment.indices) > 2)
            reflowed = limit_visible_text_lines(
                localize_speaker_labels(script, glossary)
            )
            if reflowed != script:
                document[5][0][2] = reflowed
                output_payload = json.dumps(
                    document, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                atomic_write(output_path, output_payload)
                changed_files += 1
                changed_blocks += oversized
            else:
                output_payload = payload
            translated_sha256 = sha256_bytes(output_payload)
            if item.get("translated_sha256") != translated_sha256:
                item["translated_sha256"] = translated_sha256
                manifest_changed = True
        if manifest_changed:
            atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                + b"\n",
            )
    return changed_files, changed_blocks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-root", type=Path, default=DEFAULT_GAME_ROOT)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--character", type=int, default=108)
    parser.add_argument(
        "--story-prefix",
        action="append",
        default=[],
        help="Translate every catalog story under this logical path prefix. Repeatable.",
    )
    parser.add_argument(
        "--exclude-story-prefix",
        action="append",
        default=[],
        help="Exclude a logical path prefix selected by --story-prefix. Repeatable.",
    )
    parser.add_argument(
        "--group",
        help="Stable work/manifest name used with --story-prefix (for example collaboration).",
    )
    parser.add_argument("--cdn-root", default=DEFAULT_CDN_ROOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument(
        "--reflow-existing",
        action="store_true",
        help="Rewrite all manifested translations to use at most two visible lines.",
    )
    parser.add_argument(
        "--export-partial",
        action="store_true",
        help="Export validated cached translations and keep unfinished text in Japanese.",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--batch-characters", type=int, default=18000)
    parser.add_argument("--batch-segments", type=int, default=80)
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Translate at most this many API batches before exporting a checkpoint.",
    )
    parser.add_argument(
        "--glossary",
        type=Path,
        default=Path("Lang/CHS/translation-glossary.json"),
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    if args.reflow_existing:
        changed_files, changed_blocks = reflow_existing_translations(repo_root)
        print(f"Reflowed {changed_blocks} message blocks in {changed_files} files")
        return 0

    game_root = args.game_root.resolve()
    if args.story_prefix and not args.group:
        parser.error("--group is required when --story-prefix is used")
    if args.group and not args.story_prefix:
        parser.error("--story-prefix is required when --group is used")
    if args.group and not re.fullmatch(r"[A-Za-z0-9._-]+", args.group):
        parser.error("--group may contain only letters, digits, dot, underscore, and hyphen")
    unit_name = args.group or f"character-{args.character:03d}"
    work_root = repo_root / ".work" / unit_name
    source_root = work_root / "source"
    config_path = newest_advstory_config(game_root)
    catalog = (
        load_catalog_by_prefixes(
            config_path,
            tuple(args.story_prefix),
            tuple(args.exclude_story_prefix),
        )
        if args.story_prefix
        else load_story_catalog(config_path, args.character)
    )
    print(f"Catalog: {config_path.name}; stories: {len(catalog)}")

    downloaded = 0
    source_paths: dict[str, Path] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                download_story, asset, source_root, game_root, args.cdn_root
            )
            for asset in catalog
        ]
        for completed, future in enumerate(
            concurrent.futures.as_completed(futures), start=1
        ):
            asset, path, changed = future.result()
            source_paths[asset.logical_name] = path
            downloaded += int(changed)
            if completed % 20 == 0 or completed == len(futures):
                print(f"Downloaded/verified {completed}/{len(futures)}")

    story_documents: dict[str, Any] = {}
    story_scripts: dict[str, str] = {}
    story_lines: dict[str, list[str]] = {}
    story_segments: dict[str, list[Segment]] = {}
    all_segments: list[Segment] = []
    total_source_characters = 0
    for asset in catalog:
        payload = source_paths[asset.logical_name].read_bytes()
        document, script = load_text_asset(payload, asset.logical_name)
        lines, segments = extract_segments(asset.logical_name, script)
        story_documents[asset.logical_name] = document
        story_scripts[asset.logical_name] = script
        story_lines[asset.logical_name] = lines
        story_segments[asset.logical_name] = segments
        all_segments.extend(segments)
        total_source_characters += sum(len(segment.source) for segment in segments)

    print(
        f"Segments: {len(all_segments)}; source characters: "
        f"{total_source_characters}; newly downloaded: {downloaded}"
    )
    if args.download_only:
        return 0

    glossary_path = args.glossary
    if not glossary_path.is_absolute():
        glossary_path = repo_root / glossary_path
    glossary, glossary_fingerprint = load_glossary(glossary_path)
    cache = TranslationCache(
        work_root / "translation-cache.json", args.model, glossary_fingerprint
    )
    translations: dict[str, str] = {}
    pending: list[Segment] = []
    for segment in all_segments:
        cached = cache.get(segment)
        try:
            if cached is not None:
                validate_translation(segment, cached, glossary)
        except ValueError:
            cached = None
        if cached is None:
            pending.append(segment)
        else:
            translations[segment.segment_id] = cached
    cached_translation_count = len(translations)
    if args.export_partial:
        batches: list[list[Segment]] = []
    else:
        batches = batches_by_characters(
            pending,
            max(2000, args.batch_characters),
            max(1, min(80, args.batch_segments)),
        )
        if args.max_batches > 0:
            batches = batches[: args.max_batches]
    print(
        f"Cached segments: {cached_translation_count}; pending: {len(pending)}; "
        f"API batches: {len(batches)}; model: {args.model}"
    )

    api_key = ""
    if batches:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")

    def translate_batch(batch: list[Segment]) -> tuple[list[Segment], dict[str, str]]:
        return batch, api_request(api_key, args.model, batch, glossary)

    if batches:
        workers = max(1, min(args.workers, 6, len(batches)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(translate_batch, batch) for batch in batches]
            for completed, future in enumerate(
                concurrent.futures.as_completed(futures), start=1
            ):
                batch, result = future.result()
                for segment in batch:
                    translation = result[segment.segment_id].strip()
                    validate_translation(segment, translation, glossary)
                    translations[segment.segment_id] = translation
                    cache.update(segment, translation)
                cache.save()
                print(f"Translated batch {completed}/{len(futures)}")

    remaining = [
        segment
        for segment in all_segments
        if segment.segment_id not in translations
    ]
    validated_translation_count = len(translations)
    for segment in remaining:
        translations[segment.segment_id] = segment.source

    manifest_files: list[dict[str, Any]] = []
    untranslated: list[str] = []
    for asset in catalog:
        logical_name = asset.logical_name
        lines = list(story_lines[logical_name])
        # Work backwards so a translated block may gain or lose line breaks
        # without shifting the source indices of blocks that are still pending.
        for segment in reversed(story_segments[logical_name]):
            translated = translations[segment.segment_id]
            replacement_lines = translated.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            first = segment.indices[0]
            last = segment.indices[-1]
            lines[first : last + 1] = replacement_lines
        translated_script = "\r\n".join(lines)
        if not unchanged_directives(story_scripts[logical_name], translated_script):
            raise ValueError(f"A directive changed in {logical_name}")
        translated_script = localize_speaker_labels(translated_script, glossary)
        translated_script = limit_visible_text_lines(translated_script)
        for _, segments in [extract_segments(logical_name, translated_script)]:
            for segment in segments:
                if JAPANESE_RE.search(segment.source):
                    untranslated.append(segment.segment_id)

        document = story_documents[logical_name]
        document[5][0][2] = translated_script
        output_payload = json.dumps(
            document, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        json.loads(output_payload.decode("utf-8"))
        output_path = repo_root / asset.relative_path
        atomic_write(output_path, output_payload)
        source_payload = source_paths[logical_name].read_bytes()
        manifest_files.append(
            {
                "story": logical_name,
                "path": asset.relative_path.as_posix(),
                "source_sha256": sha256_bytes(source_payload),
                "translated_sha256": sha256_bytes(output_payload),
                "segments": len(story_segments[logical_name]),
            }
        )

    manifest = {
        "format_version": 1,
        "character_id": None if args.story_prefix else args.character,
        "group": args.group,
        "story_prefixes": args.story_prefix,
        "excluded_story_prefixes": args.exclude_story_prefix,
        "model": args.model,
        "glossary_sha256": glossary_fingerprint,
        "bundle_config": config_path.name,
        "story_count": len(catalog),
        "segment_count": len(all_segments),
        "partial": bool(remaining),
        "translated_segment_count": validated_translation_count,
        "pending_segment_count": len(remaining),
        "untranslated_kana_segments": sorted(set(untranslated)),
        "files": manifest_files,
    }
    manifest_path = repo_root / "translations" / f"{unit_name}.json"
    atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )
    cache.save()
    print(
        f"Wrote {len(catalog)} translated stories; "
        f"kana warnings: {len(set(untranslated))}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
