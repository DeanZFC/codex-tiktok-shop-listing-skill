#!/usr/bin/env python3
"""Cross-platform state and safety gates for TikTok Shop listing work."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from urllib.parse import urlparse

if os.name == "nt":
    import msvcrt
else:
    import fcntl


HISTORY_VERSION = 1
EVIDENCE_VERSION = 1
PRICING_EVIDENCE_VERSION = 1
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUMBERED_RE = re.compile(r"^(\d{3})-(.+)$")
CELL_REFERENCE_RE = re.compile(r"^\$?[A-Z]{1,3}\$?[1-9]\d*$", re.IGNORECASE)
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MONEY_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,2})?$")
NUMBER_TOKEN_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")
SUPPORTED_CURRENCIES = {
    "AUD", "CAD", "CNY", "EUR", "GBP", "HKD", "IDR", "JPY", "KRW", "MYR",
    "MXN", "PHP", "PLN", "SAR", "SGD", "THB", "TRY", "TWD", "USD", "VND",
}
WINDOWS_INVALID_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
CONFIRMABLE_STATUSES = {"awaiting_manual_scan", "awaiting_miaoshou_collection"}
RECORD_LINK_STATUSES = {"awaiting_manual_scan", "awaiting_miaoshou_collection"}
MONEY_QUANTUM = Decimal("0.01")


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="microseconds")


def default_root() -> Path:
    configured = os.environ.get("TIKTOK_LISTING_WORKSPACE")
    if configured:
        return Path(configured).expanduser().resolve()
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".tiktok-shop-listing" / "config.json").is_file():
            return candidate
    return current


def example_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "config.example.json"


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def deep_merge(defaults: dict, overrides: dict) -> dict:
    merged = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def validate_http_url(value: str, field_name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be a valid HTTP(S) URL")


def require_string(config: dict, key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Config field must be a non-empty string: {key}")
    return value


def validate_portable_filename(value: str, field_name: str) -> None:
    if value in {".", ".."} or value != value.strip() or value.endswith((" ", ".")):
        raise ValueError(f"{field_name} must be a portable file name")
    if any(character in WINDOWS_INVALID_CHARS or ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} must not contain path separators or invalid characters")
    if Path(value).name != value or Path(value).is_absolute():
        raise ValueError(f"{field_name} must be a file name, not a path")
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{field_name} uses a Windows-reserved file name")


def validate_config(config: dict) -> None:
    if type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("Unsupported config version")
    roots = config.get("catalog_roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(item, str) and item for item in roots):
        raise ValueError("catalog_roots must contain at least one path")
    require_string(config, "output_root")
    require_string(config, "state_root")

    discovery = config.get("product_discovery")
    if not isinstance(discovery, dict):
        raise ValueError("product_discovery must be an object")
    extensions = discovery.get("image_extensions")
    if not isinstance(extensions, list) or not extensions or not all(
        isinstance(item, str) and item.startswith(".") for item in extensions
    ):
        raise ValueError("product_discovery.image_extensions must contain file suffixes")
    ignored = discovery.get("ignore_directories")
    if not isinstance(ignored, list) or not all(isinstance(item, str) for item in ignored):
        raise ValueError("product_discovery.ignore_directories must be a string list")
    minimum = discovery.get("min_product_code_chars")
    if type(minimum) is not int or minimum < 2:
        raise ValueError("product_discovery.min_product_code_chars must be at least 2")

    availability = config.get("availability")
    if not isinstance(availability, dict) or not isinstance(availability.get("required"), bool):
        raise ValueError("availability.required must be true or false")
    document_url = availability.get("document_url")
    if not isinstance(document_url, str):
        raise ValueError("availability.document_url must be a string")
    if document_url:
        validate_http_url(document_url, "availability.document_url")
    bound_catalog_roots = availability.get("bound_catalog_roots")
    if not isinstance(bound_catalog_roots, list) or not all(
        isinstance(item, str) and item for item in bound_catalog_roots
    ):
        raise ValueError("availability.bound_catalog_roots must be a string list")
    if availability.get("scope") != "all_sheets":
        raise ValueError("availability.scope must be all_sheets")
    sold_out = availability.get("sold_out_sheets")
    if not isinstance(sold_out, list) or not all(isinstance(item, str) and item for item in sold_out):
        raise ValueError("availability.sold_out_sheets must be a string list")
    fresh_minutes = availability.get("fresh_minutes")
    if type(fresh_minutes) is not int or fresh_minutes < 1:
        raise ValueError("availability.fresh_minutes must be a positive integer")

    collection = config.get("collection")
    if not isinstance(collection, dict):
        raise ValueError("collection must be an object")
    minimum_links = collection.get("min_links")
    maximum_links = collection.get("max_links")
    if type(minimum_links) is not int or type(maximum_links) is not int:
        raise ValueError("collection link limits must be integers")
    if minimum_links < 1 or maximum_links < minimum_links:
        raise ValueError("collection link limits are invalid")
    if type(collection.get("final_image_count")) is not int or collection["final_image_count"] < 1:
        raise ValueError("collection.final_image_count must be positive")
    if not isinstance(collection.get("price_threshold"), (int, float)) or isinstance(
        collection["price_threshold"], bool
    ) or not math.isfinite(collection["price_threshold"]):
        raise ValueError("collection.price_threshold must be numeric")
    if collection["price_threshold"] != 50:
        raise ValueError("collection.price_threshold must remain 50.0")
    if not isinstance(collection.get("minimum_profit_rate"), (int, float)) or isinstance(
        collection["minimum_profit_rate"], bool
    ) or not math.isfinite(collection["minimum_profit_rate"]):
        raise ValueError("collection.minimum_profit_rate must be numeric")
    if not 0.05 <= collection["minimum_profit_rate"] < 1:
        raise ValueError("collection.minimum_profit_rate must be at least 0.05 and less than 1")
    if type(collection.get("sku_inventory")) is not int or collection["sku_inventory"] < 0:
        raise ValueError("collection.sku_inventory must be a non-negative integer")
    if collection.get("browser") != "edge":
        raise ValueError("collection.browser must be edge")
    if not isinstance(collection.get("delete_nonselected"), bool):
        raise ValueError("collection.delete_nonselected must be true or false")

    copy = config.get("copy")
    if not isinstance(copy, dict):
        raise ValueError("copy must be an object")
    if not isinstance(copy.get("language"), str) or not copy["language"].strip():
        raise ValueError("copy.language must be a non-empty string")
    if type(copy.get("title_max_chars")) is not int or copy["title_max_chars"] < 1:
        raise ValueError("copy.title_max_chars must be a positive integer")
    preferred_fields = copy.get("preferred_fields")
    if not isinstance(preferred_fields, list) or not all(
        isinstance(value, str) and value.strip() for value in preferred_fields
    ):
        raise ValueError("copy.preferred_fields must be a string list")

    deliverables = config.get("deliverables")
    if not isinstance(deliverables, dict):
        raise ValueError("deliverables must be an object")
    for field in ("copy_file", "scan_image", "spec_chart", "supplier_links_file"):
        value = require_string(deliverables, field)
        validate_portable_filename(value, f"deliverables.{field}")
    filenames = [
        normalize_identifier(deliverables[field])
        for field in ("copy_file", "scan_image", "spec_chart", "supplier_links_file")
    ]
    if len(set(filenames)) != len(filenames):
        raise ValueError("deliverable file names must be unique")
    if Path(deliverables["copy_file"]).suffix.casefold() != ".txt":
        raise ValueError("deliverables.copy_file must use a .txt extension")
    if Path(deliverables["scan_image"]).suffix.casefold() not in {".jpg", ".jpeg"}:
        raise ValueError("deliverables.scan_image must use a .jpg or .jpeg extension")
    if Path(deliverables["spec_chart"]).suffix.casefold() != ".png":
        raise ValueError("deliverables.spec_chart must use a .png extension")
    if Path(deliverables["supplier_links_file"]).suffix.casefold() != ".txt":
        raise ValueError("deliverables.supplier_links_file must use a .txt extension")
    if not isinstance(deliverables.get("spec_chart_required"), bool):
        raise ValueError("deliverables.spec_chart_required must be true or false")


def config_path_for(root: Path, value: str | None) -> Path:
    if value:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else root / path).resolve()
    return (root / ".tiktok-shop-listing" / "config.json").resolve()


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Config does not exist: {path}. Run init-config first")
    defaults = read_json(example_config_path())
    config = deep_merge(defaults, read_json(path))
    config.get("collection", {}).pop("price_replacement", None)
    validate_config(config)
    return config


def workspace_path(root: Path, value: str, field_name: str) -> Path:
    raw = Path(value).expanduser()
    resolved = (raw if raw.is_absolute() else root / raw).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{field_name} must remain inside the workspace")
    return resolved


def catalog_paths(root: Path, config: dict) -> list[Path]:
    paths = []
    for value in config["catalog_roots"]:
        raw = Path(value).expanduser()
        paths.append((raw if raw.is_absolute() else root / raw).resolve())
    return paths


def build_context(root: Path, config_path: Path, config: dict) -> dict:
    state_root = workspace_path(root, config["state_root"], "state_root")
    output_root = workspace_path(root, config["output_root"], "output_root")
    return {
        "root": root,
        "config_path": config_path,
        "config": config,
        "catalogs": catalog_paths(root, config),
        "output_root": output_root,
        "state_root": state_root,
        "history": state_root / "history.json",
        "lock": state_root / "history.lock",
    }


def config_fingerprint(config: dict) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def availability_bound_catalog_paths(root: Path, config: dict) -> list[Path]:
    paths = []
    for value in config["availability"]["bound_catalog_roots"]:
        raw = Path(value).expanduser()
        paths.append((raw if raw.is_absolute() else root / raw).resolve())
    return paths


def availability_fingerprint(context: dict) -> str:
    availability = context["config"]["availability"]
    payload = {
        "document_url": availability["document_url"],
        "bound_catalog_roots": [
            str(path) for path in availability_bound_catalog_paths(context["root"], context["config"])
        ],
        "scope": availability["scope"],
        "sold_out_sheets": availability["sold_out_sheets"],
        "fresh_minutes": availability["fresh_minutes"],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def pricing_config_fingerprint(config: dict) -> str:
    collection = config["collection"]
    payload = {
        "price_threshold": str(collection["price_threshold"]),
        "minimum_profit_rate": str(collection["minimum_profit_rate"]),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def empty_history() -> dict:
    return {"version": HISTORY_VERSION, "updated_at": now_iso(), "products": []}


def load_history(path: Path) -> dict:
    if not path.exists():
        return empty_history()
    history = read_json(path)
    if history.get("version") != HISTORY_VERSION or not isinstance(history.get("products"), list):
        raise ValueError(f"Unsupported history format: {path}")
    return history


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = now_iso()
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


@contextmanager
def locked(context: dict):
    context["state_root"].mkdir(parents=True, exist_ok=True)
    with context["lock"].open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def normalize_identifier(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def validate_identifier(value: str, minimum: int, field_name: str) -> str:
    cleaned = unicodedata.normalize("NFKC", value).strip()
    if len(cleaned) < minimum:
        raise ValueError(f"{field_name} must contain at least {minimum} characters")
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError(f"{field_name} contains control characters")
    return cleaned


def portable_component(value: str) -> str:
    cleaned = "".join(
        "-" if character in WINDOWS_INVALID_CHARS or ord(character) < 32 else character
        for character in unicodedata.normalize("NFKC", value).strip()
    ).rstrip(" .")
    cleaned = re.sub(r"-+", "-", cleaned)
    if not cleaned:
        raise ValueError("Product code cannot form a portable folder name")
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"item-{cleaned}"
    return cleaned


def validate_date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("Date must use YYYY-MM-DD")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def source_reference(source: Path, catalogs: list[Path]) -> dict:
    matches = []
    for index, catalog in enumerate(catalogs):
        if source.is_relative_to(catalog):
            matches.append((len(catalog.parts), index, source.relative_to(catalog)))
    if not matches:
        raise ValueError("Source directory must be inside a configured catalog root")
    _, index, relative = max(matches, key=lambda item: (item[0], item[1]))
    return {"catalog_index": index, "relative_path": relative.as_posix()}


def source_ref_key(reference: dict) -> str:
    return f"{reference['catalog_index']}:{reference['relative_path']}"


def filesystem_identity(path: Path) -> tuple:
    resolved = path.resolve()
    try:
        stat = resolved.stat()
        if stat.st_ino:
            return ("filesystem", stat.st_dev, stat.st_ino)
    except OSError:
        pass
    normalized = unicodedata.normalize("NFKC", os.path.normcase(str(resolved)))
    return ("path", normalized.casefold() if os.name == "nt" else normalized)


def source_path_from_entry(entry: dict, context: dict) -> Path:
    reference = entry["source"]
    index = reference["catalog_index"]
    if not isinstance(index, int) or not 0 <= index < len(context["catalogs"]):
        raise ValueError("History source catalog index is invalid")
    catalog = context["catalogs"][index]
    source = (catalog / reference["relative_path"]).resolve()
    if not source.is_relative_to(catalog):
        raise ValueError("History source path escapes its configured catalog root")
    return source


def output_path_from_entry(entry: dict, context: dict) -> Path:
    output = (context["root"] / entry["output_relative"]).resolve()
    if not output.is_relative_to(context["output_root"]):
        raise ValueError("History output path escapes the configured output root")
    return output


def selected_codes(history: dict) -> dict[str, dict]:
    return {entry["canonical_product_code"]: entry for entry in history["products"]}


def selected_identifiers(history: dict) -> dict[str, dict]:
    identifiers = {}
    for entry in history["products"]:
        values = [entry["canonical_product_code"], *entry.get("product_code_aliases", [])]
        for value in values:
            identifiers.setdefault(normalize_identifier(value), entry)
    return identifiers


def find_entry_by_output(history: dict, output_dir: Path, context: dict) -> dict:
    target = output_dir.resolve()
    for entry in history["products"]:
        if output_path_from_entry(entry, context) == target:
            return entry
    raise ValueError(f"No selected product is recorded for output directory: {output_dir}")


def iter_candidates(context: dict) -> list[dict]:
    discovery = context["config"]["product_discovery"]
    suffixes = {value.casefold() for value in discovery["image_extensions"]}
    ignored = set(discovery["ignore_directories"])
    candidates = []
    for index, catalog in enumerate(context["catalogs"]):
        if not catalog.is_dir():
            continue
        counts: collections.Counter[Path] = collections.Counter()
        for path in catalog.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in suffixes:
                continue
            relative = path.parent.relative_to(catalog)
            if any(part in ignored for part in relative.parts):
                continue
            counts[path.parent.resolve()] += 1
        for directory, image_count in counts.items():
            relative = directory.relative_to(catalog)
            reference = {"catalog_index": index, "relative_path": relative.as_posix()}
            candidates.append(
                {
                    "candidate_name": directory.name,
                    "source_dir": str(directory),
                    "source_ref": source_ref_key(reference),
                    "image_count": image_count,
                }
            )
    return sorted(candidates, key=lambda item: (item["source_ref"], item["candidate_name"]))


def command_init_config(args: argparse.Namespace, root: Path, config_path: Path) -> int:
    if config_path.exists():
        raise ValueError(f"Config already exists: {config_path}")
    config = read_json(example_config_path())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(config_path, config)
    print(json.dumps({"config": str(config_path), "created": True}, ensure_ascii=False, indent=2))
    return 0


def command_show_config(args: argparse.Namespace, context: dict) -> int:
    payload = {
        "workspace_root": str(context["root"]),
        "config_path": str(context["config_path"]),
        "config_fingerprint": config_fingerprint(context["config"]),
        "config": context["config"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_eligible(args: argparse.Namespace, context: dict) -> int:
    missing_catalogs = [str(path) for path in context["catalogs"] if not path.is_dir()]
    if missing_catalogs:
        raise ValueError(f"Configured catalog roots are missing or not directories: {missing_catalogs}")
    history = load_history(context["history"])
    used_sources = {
        filesystem_identity(source_path_from_entry(entry, context)) for entry in history["products"]
    }
    seen_sources = set()
    items = []
    for item in iter_candidates(context):
        source = Path(item["source_dir"]).resolve()
        identity = filesystem_identity(source)
        if identity in used_sources or identity in seen_sources:
            continue
        seen_sources.add(identity)
        items.append(item)
    total = len(items)
    if args.limit:
        items = items[: args.limit]
    print(
        json.dumps(
            {
                "history_file": str(context["history"]),
                "eligible_total": total,
                "returned_count": len(items),
                "products": items,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_reserve(args: argparse.Namespace, context: dict) -> int:
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    reference = source_reference(source, context["catalogs"])
    discovery = context["config"]["product_discovery"]
    suffixes = {value.casefold() for value in discovery["image_extensions"]}
    if not any(path.is_file() and path.suffix.casefold() in suffixes for path in source.iterdir()):
        raise ValueError("Source product directory must contain at least one supported image")

    minimum = discovery["min_product_code_chars"]
    product_code = validate_identifier(args.product_code, minimum, "product_code")
    canonical = normalize_identifier(product_code)
    aliases = []
    for value in args.aliases:
        alias = validate_identifier(value, minimum, "product-code alias")
        if normalize_identifier(alias) not in {canonical, *(normalize_identifier(item) for item in aliases)}:
            aliases.append(alias)
    selected_date = args.date or dt.date.today().isoformat()

    with locked(context):
        history = load_history(context["history"])
        requested_identifiers = {canonical, *(normalize_identifier(item) for item in aliases)}
        identifiers = selected_identifiers(history)
        collision = next(
            ((identifier, identifiers[identifier]) for identifier in requested_identifiers if identifier in identifiers),
            None,
        )
        if collision:
            identifier, existing = collision
            print(
                json.dumps(
                    {"error": "already_selected", "identifier": identifier, "entry": existing},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
        existing_source = next(
            (
                item
                for item in history["products"]
                if filesystem_identity(source_path_from_entry(item, context))
                == filesystem_identity(source)
            ),
            None,
        )
        if existing_source:
            print(
                json.dumps(
                    {"error": "source_already_selected", "entry": existing_source},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2

        date_dir = context["output_root"] / selected_date
        date_dir.mkdir(parents=True, exist_ok=True)
        used_numbers = []
        for child in date_dir.iterdir():
            match = NUMBERED_RE.match(child.name)
            if child.is_dir() and match:
                used_numbers.append(int(match.group(1)))
        for previous in history["products"]:
            if previous.get("selected_date") != selected_date:
                continue
            match = NUMBERED_RE.match(Path(previous["output_relative"]).name)
            if match:
                used_numbers.append(int(match.group(1)))
        number = max(used_numbers, default=0) + 1
        output_dir = date_dir / f"{number:03d}-{portable_component(product_code)}"
        output_dir.mkdir()

        entry = {
            "product_code": product_code,
            "canonical_product_code": canonical,
            "product_code_aliases": aliases,
            "category": args.category,
            "tags": args.tags,
            "selected_date": selected_date,
            "recorded_at": now_iso(),
            "source": reference,
            "output_relative": output_dir.relative_to(context["root"]).as_posix(),
            "status": "awaiting_manual_scan",
        }
        history["products"].append(entry)
        history["products"].sort(key=lambda item: (item["selected_date"], item["product_code"]))
        try:
            atomic_write_json(context["history"], history)
        except Exception:
            output_dir.rmdir()
            raise

    print(
        json.dumps(
            {
                "history_file": str(context["history"]),
                "output_dir": str(output_dir),
                "entry": entry,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_reset(args: argparse.Namespace, context: dict) -> int:
    if args.confirm != "CLEAR":
        raise ValueError("Reset requires --confirm CLEAR")
    with locked(context):
        previous = load_history(context["history"])
        history = empty_history()
        history["reset_at"] = now_iso()
        history["reset_previous_count"] = len(previous["products"])
        atomic_write_json(context["history"], history)
    print(
        json.dumps(
            {"history_file": str(context["history"]), "cleared": True, "previous_count": len(previous["products"])},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def validate_urls(values: list[str]) -> list[str]:
    urls = []
    for value in values:
        validate_http_url(value, "supplier link")
        urls.append(value)
    return urls


def allowed_queries(entry: dict) -> set[str]:
    values = [entry["product_code"], *entry.get("product_code_aliases", [])]
    return {normalize_identifier(value) for value in values}


def text_contains_exact_query(text: str, query: str) -> bool:
    normalized_text = normalize_identifier(text)
    normalized_query = normalize_identifier(query)
    pattern = rf"(?<![A-Za-z0-9]){re.escape(normalized_query)}(?![A-Za-z0-9])"
    return re.search(pattern, normalized_text) is not None


def parse_timestamp(value: str, field_name: str) -> dt.datetime:
    try:
        timestamp = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name} timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"{field_name} timestamp must include a timezone")
    return timestamp.astimezone(dt.timezone.utc)


def parse_money(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str) or not MONEY_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a positive decimal string with at most two places")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is not a valid decimal amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return amount.quantize(MONEY_QUANTUM)


def decimal_from_config(value: object, field_name: str) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is not a valid decimal") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return decimal_value


def minimum_price_for_cost(cost: Decimal, profit_rate: Decimal) -> Decimal:
    return (cost * (Decimal("1") + profit_rate)).quantize(
        MONEY_QUANTUM, rounding=ROUND_CEILING
    )


def money_text(value: Decimal) -> str:
    return format(value.quantize(MONEY_QUANTUM), ".2f")


def raw_text_uniquely_represents_amount(raw_text: str, amount: Decimal) -> bool:
    if "%" in raw_text or re.search(r"(?<!\d)-\s*\d", raw_text):
        return False
    tokens = NUMBER_TOKEN_RE.findall(raw_text.replace(",", ""))
    if len(tokens) != 1:
        return False
    try:
        return Decimal(tokens[0]) == amount
    except InvalidOperation:
        return False


def pricing_evidence_payload(
    entry: dict,
    context: dict,
    currency: str,
    sales_unit: str,
    expected_sku_count: int,
    skus: list[dict],
) -> dict:
    needs_cost = any("supplier_cost" in sku for sku in skus)
    availability = None
    if context["config"]["availability"]["required"] or needs_cost:
        availability = require_availability(entry, context, require_fresh=True)
    recorded_urls = entry.get("supplier_links")
    links_recorded_at = entry.get("links_recorded_at")
    if not isinstance(recorded_urls, list) or not recorded_urls:
        raise ValueError("Pricing requires recorded supplier links")
    parse_timestamp(links_recorded_at, "supplier links")
    if availability is not None and (
        availability.get("recorded_links") != recorded_urls
        or availability.get("recorded_links_at") != links_recorded_at
    ):
        raise ValueError("Availability evidence is not bound to the current supplier links")
    collection = context["config"]["collection"]
    threshold = decimal_from_config(collection["price_threshold"], "collection.price_threshold")
    profit_rate = decimal_from_config(
        collection["minimum_profit_rate"], "collection.minimum_profit_rate"
    )
    return {
        "schema_version": PRICING_EVIDENCE_VERSION,
        "confirmed_at": now_iso(),
        "currency": currency,
        "sales_unit": sales_unit,
        "expected_sku_count": expected_sku_count,
        "product_canonical_code": entry["canonical_product_code"],
        "availability_required": availability is not None,
        "availability_checked_at": availability["checked_at"] if availability else None,
        "availability_config_fingerprint": availability["config_fingerprint"] if availability else None,
        "recorded_links": list(recorded_urls),
        "links_recorded_at": links_recorded_at,
        "pricing_config_fingerprint": pricing_config_fingerprint(context["config"]),
        "price_threshold": money_text(threshold),
        "minimum_profit_rate": format(profit_rate, "f"),
        "skus": skus,
    }


def normalize_pricing_skus(
    data: dict,
    entry: dict,
    context: dict,
    *,
    currency: str,
    sales_unit: str,
) -> list[dict]:
    raw_skus = data.get("skus")
    if not isinstance(raw_skus, list) or not raw_skus:
        raise ValueError("sku-file must contain a non-empty skus list")
    collection = context["config"]["collection"]
    threshold = decimal_from_config(collection["price_threshold"], "collection.price_threshold")
    profit_rate = decimal_from_config(
        collection["minimum_profit_rate"], "collection.minimum_profit_rate"
    )
    seen = set()
    normalized = []
    for index, raw_sku in enumerate(raw_skus, start=1):
        prefix = f"skus[{index}]"
        if not isinstance(raw_sku, dict):
            raise ValueError(f"{prefix} must be an object")
        sku_key = raw_sku.get("sku_key")
        if not isinstance(sku_key, str) or not sku_key.strip():
            raise ValueError(f"{prefix}.sku_key must be a non-empty string")
        cleaned_key = unicodedata.normalize("NFKC", sku_key).strip()
        canonical_key = normalize_identifier(cleaned_key)
        if canonical_key in seen:
            raise ValueError("SKU keys must be unique after normalization")
        seen.add(canonical_key)
        original = parse_money(raw_sku.get("original_price"), f"{prefix}.original_price")
        final = parse_money(raw_sku.get("final_price"), f"{prefix}.final_price")
        item = {
            "sku_key": cleaned_key,
            "canonical_sku_key": canonical_key,
            "original_price": money_text(original),
            "final_price": money_text(final),
        }
        if original >= threshold:
            if final != original:
                raise ValueError(f"{prefix}.final_price must preserve an original price at or above threshold")
        else:
            cost = parse_money(raw_sku.get("supplier_cost"), f"{prefix}.supplier_cost")
            cost_sheet = raw_sku.get("cost_sheet")
            cost_cell = raw_sku.get("cost_cell")
            cost_raw_text = raw_sku.get("cost_raw_text")
            supplier_currency = raw_sku.get("supplier_currency")
            supplier_unit = raw_sku.get("supplier_unit")
            supplier_sku_text = raw_sku.get("supplier_sku_text")
            product_query = raw_sku.get("product_query")
            product_cell = raw_sku.get("product_cell")
            product_raw_text = raw_sku.get("product_raw_text")
            supplier_sku_cell = raw_sku.get("supplier_sku_cell")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    cost_sheet,
                    cost_cell,
                    cost_raw_text,
                    supplier_currency,
                    supplier_unit,
                    supplier_sku_text,
                    product_query,
                    product_cell,
                    product_raw_text,
                    supplier_sku_cell,
                )
            ):
                raise ValueError(f"{prefix} requires complete supplier cost evidence")
            cleaned_supplier_currency = supplier_currency.strip().upper()
            if cleaned_supplier_currency not in SUPPORTED_CURRENCIES:
                raise ValueError(f"{prefix}.supplier_currency is not a supported ISO 4217 code")
            if cleaned_supplier_currency != currency:
                raise ValueError(f"{prefix}.supplier_currency must match the Miaoshou price currency")
            cleaned_supplier_unit = unicodedata.normalize("NFKC", supplier_unit).strip()
            if normalize_identifier(cleaned_supplier_unit) != normalize_identifier(sales_unit):
                raise ValueError(f"{prefix}.supplier_unit must match the Miaoshou sales unit")
            cleaned_supplier_sku_text = unicodedata.normalize("NFKC", supplier_sku_text).strip()
            if normalize_identifier(cleaned_supplier_sku_text) != canonical_key:
                raise ValueError(f"{prefix}.supplier_sku_text must exactly match sku_key")
            cleaned_cell = cost_cell.strip().upper()
            cleaned_product_cell = product_cell.strip().upper()
            cleaned_supplier_sku_cell = supplier_sku_cell.strip().upper()
            for field_name, cell in (
                ("cost_cell", cleaned_cell),
                ("product_cell", cleaned_product_cell),
                ("supplier_sku_cell", cleaned_supplier_sku_cell),
            ):
                if not CELL_REFERENCE_RE.fullmatch(cell):
                    raise ValueError(f"{prefix}.{field_name} must be an A1-style cell reference")
            cell_rows = {
                re.search(r"[1-9]\d*$", cell).group()
                for cell in (cleaned_cell, cleaned_product_cell, cleaned_supplier_sku_cell)
            }
            if len(cell_rows) != 1:
                raise ValueError(f"{prefix} product, SKU, and cost cells must be on the same row")
            cleaned_sheet = cost_sheet.strip()
            if cleaned_sheet in context["config"]["availability"]["sold_out_sheets"]:
                raise ValueError(f"{prefix}.cost_sheet cannot be a sold-out worksheet")
            cleaned_raw_text = cost_raw_text.strip()
            if not raw_text_uniquely_represents_amount(cleaned_raw_text, cost):
                raise ValueError(
                    f"{prefix}.cost_raw_text must contain one unambiguous amount equal to supplier_cost"
                )
            cleaned_product_query = unicodedata.normalize("NFKC", product_query).strip()
            if normalize_identifier(cleaned_product_query) not in allowed_queries(entry):
                raise ValueError(f"{prefix}.product_query must be the recorded product code or alias")
            cleaned_product_raw_text = unicodedata.normalize("NFKC", product_raw_text).strip()
            if not text_contains_exact_query(cleaned_product_raw_text, cleaned_product_query):
                raise ValueError(f"{prefix}.product_raw_text must contain the complete product query")
            currency_codes = {
                match.upper()
                for match in re.findall(
                    r"(?<![A-Za-z])[A-Za-z]{3}(?![A-Za-z])", cleaned_raw_text
                )
                if match.upper() in SUPPORTED_CURRENCIES
            }
            if currency_codes and currency_codes != {cleaned_supplier_currency}:
                raise ValueError(f"{prefix}.cost_raw_text currency conflicts with supplier_currency")
            floor = minimum_price_for_cost(cost, profit_rate)
            required = max(original, floor)
            if final != required:
                raise ValueError(
                    f"{prefix}.final_price must be {money_text(required)} "
                    f"(max of original price and supplier cost plus configured markup)"
                )
            item.update(
                {
                    "supplier_cost": money_text(cost),
                    "minimum_price": money_text(floor),
                    "cost_sheet": cleaned_sheet,
                    "cost_cell": cleaned_cell,
                    "cost_raw_text": cleaned_raw_text,
                    "supplier_currency": cleaned_supplier_currency,
                    "supplier_unit": cleaned_supplier_unit,
                    "supplier_sku_text": cleaned_supplier_sku_text,
                    "product_query": cleaned_product_query,
                    "product_cell": cleaned_product_cell,
                    "product_raw_text": cleaned_product_raw_text,
                    "supplier_sku_cell": cleaned_supplier_sku_cell,
                }
            )
        allowed_fields = {"sku_key", "canonical_sku_key", "original_price", "final_price"}
        if original < threshold:
            allowed_fields.update(
                {
                    "supplier_cost",
                    "cost_sheet",
                    "cost_cell",
                    "cost_raw_text",
                    "supplier_currency",
                    "supplier_unit",
                    "supplier_sku_text",
                    "product_query",
                    "product_cell",
                    "product_raw_text",
                    "supplier_sku_cell",
                    "minimum_price",
                }
            )
        unknown_fields = set(raw_sku) - allowed_fields
        if unknown_fields:
            raise ValueError(f"{prefix} contains unsupported fields: {sorted(unknown_fields)}")
        if "canonical_sku_key" in raw_sku and raw_sku["canonical_sku_key"] != canonical_key:
            raise ValueError(f"{prefix}.canonical_sku_key does not match sku_key")
        if original < threshold and "minimum_price" in raw_sku:
            minimum_price = parse_money(raw_sku["minimum_price"], f"{prefix}.minimum_price")
            if minimum_price != floor:
                raise ValueError(f"{prefix}.minimum_price does not match supplier cost")
        normalized.append(item)
    return normalized


def require_pricing(entry: dict, context: dict) -> dict:
    evidence = entry.get("pricing_confirmation")
    if not isinstance(evidence, dict):
        raise ValueError("Current product has no confirmed SKU pricing")
    expected = {
        "schema_version": PRICING_EVIDENCE_VERSION,
        "product_canonical_code": entry["canonical_product_code"],
        "recorded_links": entry.get("supplier_links"),
        "links_recorded_at": entry.get("links_recorded_at"),
        "pricing_config_fingerprint": pricing_config_fingerprint(context["config"]),
        "price_threshold": money_text(
            decimal_from_config(
                context["config"]["collection"]["price_threshold"],
                "collection.price_threshold",
            )
        ),
        "minimum_profit_rate": format(
            decimal_from_config(
                context["config"]["collection"]["minimum_profit_rate"],
                "collection.minimum_profit_rate",
            ),
            "f",
        ),
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            raise ValueError(f"Invalid pricing evidence field: {field}")
    currency = evidence.get("currency")
    if not isinstance(currency, str) or currency not in SUPPORTED_CURRENCIES:
        raise ValueError("Pricing evidence currency must be a supported ISO 4217 code")
    sales_unit = evidence.get("sales_unit")
    if not isinstance(sales_unit, str) or not sales_unit.strip():
        raise ValueError("Pricing evidence sales unit must be a non-empty string")
    expected_sku_count = evidence.get("expected_sku_count")
    if type(expected_sku_count) is not int or expected_sku_count < 1:
        raise ValueError("Pricing evidence expected SKU count must be positive")
    parse_timestamp(evidence.get("confirmed_at"), "pricing confirmation")
    try:
        normalized_skus = normalize_pricing_skus(
            {"skus": evidence.get("skus")},
            entry,
            context,
            currency=currency,
            sales_unit=sales_unit,
        )
    except ValueError as exc:
        raise ValueError(f"Invalid pricing evidence: {exc}") from exc
    if evidence.get("skus") != normalized_skus:
        raise ValueError("Pricing evidence SKU data is not canonical or has been modified")
    if len(normalized_skus) != expected_sku_count:
        raise ValueError("Pricing evidence does not cover the declared retained SKU count")
    needs_cost = any("supplier_cost" in sku for sku in normalized_skus)
    should_have_availability = context["config"]["availability"]["required"] or needs_cost
    if evidence.get("availability_required") is not should_have_availability:
        raise ValueError("Pricing evidence availability binding is invalid")
    if should_have_availability:
        availability = require_availability(entry, context, require_fresh=True)
        if evidence.get("availability_checked_at") != availability["checked_at"]:
            raise ValueError("Pricing evidence is not bound to the current availability check")
        if evidence.get("availability_config_fingerprint") != availability["config_fingerprint"]:
            raise ValueError("Pricing evidence is not bound to the current supplier document")
    elif evidence.get("availability_checked_at") is not None or evidence.get(
        "availability_config_fingerprint"
    ) is not None:
        raise ValueError("Pricing evidence has an unexpected availability binding")
    return evidence


def require_availability(entry: dict, context: dict, *, require_fresh: bool) -> dict:
    config = context["config"]
    availability = config["availability"]
    evidence = entry.get("availability")
    if not isinstance(evidence, dict) or evidence.get("confirmed") is not True:
        raise ValueError("Current product has no confirmed availability check")
    expected = {
        "schema_version": EVIDENCE_VERSION,
        "product_canonical_code": entry["canonical_product_code"],
        "document_url": availability["document_url"],
        "config_fingerprint": availability_fingerprint(context),
        "scope": "all_sheets",
        "sold_out_sheets": availability["sold_out_sheets"],
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            raise ValueError(f"Invalid availability evidence field: {field}")
    if evidence.get("all_sheets_checked") is not True or evidence.get("sold_out_match") is not False:
        raise ValueError("Availability evidence does not clear all worksheets and sold-out sheets")
    if evidence.get("match_type") != "exact_product_code":
        raise ValueError("Availability match type must be exact_product_code")
    if not all(
        isinstance(evidence.get(field), str) and evidence[field].strip()
        for field in ("query", "matched_text", "active_sheet", "active_cell", "checked_at")
    ):
        raise ValueError("Availability evidence is incomplete")
    if evidence["active_sheet"] in availability["sold_out_sheets"]:
        raise ValueError("A sold-out worksheet cannot prove availability")
    if not CELL_REFERENCE_RE.fullmatch(evidence["active_cell"]):
        raise ValueError("Availability cell reference is invalid")
    if normalize_identifier(evidence["query"]) not in allowed_queries(entry):
        raise ValueError("Availability query is not a recorded product code or alias")
    if not text_contains_exact_query(evidence["matched_text"], evidence["query"]):
        raise ValueError("Availability matched text does not contain the complete product code")
    checked_at = parse_timestamp(evidence["checked_at"], "availability check")
    age = dt.datetime.now(dt.timezone.utc) - checked_at
    if age < -dt.timedelta(minutes=5):
        raise ValueError("Availability check timestamp is in the future")
    if require_fresh and age > dt.timedelta(minutes=availability["fresh_minutes"]):
        raise ValueError("Availability evidence is stale; check the document again")
    return evidence


def availability_setup_errors(context: dict, *, force: bool = False) -> list[str]:
    config = context["config"]
    availability = config["availability"]
    if not availability["required"] and not force:
        return []
    errors = []
    if not availability["document_url"]:
        errors.append(
            "availability.document_url is empty; ask the user for the current category supplier document"
        )
    bound_catalogs = availability_bound_catalog_paths(context["root"], config)
    if bound_catalogs != context["catalogs"]:
        errors.append(
            "availability.bound_catalog_roots does not match catalog_roots; after changing category, "
            "ask the user for its supplier document and bind it to the current catalog_roots"
        )
    return errors


def require_availability_setup(context: dict, *, force: bool = False) -> None:
    errors = availability_setup_errors(context, force=force)
    if errors:
        raise ValueError("; ".join(errors))


def command_confirm_availability(args: argparse.Namespace, context: dict) -> int:
    config = context["config"]
    availability = config["availability"]
    if not availability["required"]:
        raise ValueError("Availability checks are disabled in config")
    require_availability_setup(context)
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not output_dir.is_dir() or not output_dir.is_relative_to(context["output_root"]):
        raise ValueError("Output directory must exist inside output_root")
    query = args.query.strip()
    matched_text = args.matched_text.strip()
    active_sheet = args.active_sheet.strip()
    active_cell = args.active_cell.strip().upper()
    if not CELL_REFERENCE_RE.fullmatch(active_cell):
        raise ValueError("active-cell must be an A1-style cell reference")
    if active_sheet in availability["sold_out_sheets"]:
        raise ValueError("A sold-out worksheet cannot prove availability")

    with locked(context):
        history = load_history(context["history"])
        entry = find_entry_by_output(history, output_dir, context)
        if entry.get("status") not in CONFIRMABLE_STATUSES:
            raise ValueError(f"Product status cannot be availability-confirmed: {entry.get('status')}")
        if normalize_identifier(query) not in allowed_queries(entry):
            raise ValueError("query must be the recorded product code or an explicit alias")
        if not text_contains_exact_query(matched_text, query):
            raise ValueError("matched-text does not contain the complete product code")
        evidence = {
            "schema_version": EVIDENCE_VERSION,
            "confirmed": True,
            "product_canonical_code": entry["canonical_product_code"],
            "document_url": availability["document_url"],
            "config_fingerprint": availability_fingerprint(context),
            "query": query,
            "matched_text": matched_text,
            "match_type": "exact_product_code",
            "scope": "all_sheets",
            "all_sheets_checked": True,
            "active_sheet": active_sheet,
            "active_cell": active_cell,
            "sold_out_sheets": availability["sold_out_sheets"],
            "sold_out_match": False,
            "checked_at": now_iso(),
        }
        if entry.get("status") == "awaiting_miaoshou_collection" and entry.get("supplier_links"):
            evidence["recorded_links"] = list(entry["supplier_links"])
            evidence["recorded_links_at"] = entry.get("links_recorded_at")
        entry["availability"] = evidence
        entry.pop("pricing_confirmation", None)
        atomic_write_json(context["history"], history)
    print(json.dumps({"history_file": str(context["history"]), "entry": entry}, ensure_ascii=False, indent=2))
    return 0


def command_record_links(args: argparse.Namespace, context: dict) -> int:
    config = context["config"]
    require_availability_setup(context)
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not output_dir.is_dir() or not output_dir.is_relative_to(context["output_root"]):
        raise ValueError("Output directory must exist inside output_root")
    urls = validate_urls(args.urls)
    collection = config["collection"]
    if not collection["min_links"] <= len(urls) <= collection["max_links"]:
        raise ValueError("Supplier link count is outside configured limits")

    with locked(context):
        history = load_history(context["history"])
        entry = find_entry_by_output(history, output_dir, context)
        if entry.get("status") not in RECORD_LINK_STATUSES:
            raise ValueError(f"Product status cannot record links: {entry.get('status')}")
        evidence = None
        if config["availability"]["required"]:
            evidence = require_availability(entry, context, require_fresh=True)
        previous_urls = entry.get("supplier_links", [])
        if previous_urls and previous_urls != urls and evidence is not None:
            checked_at = parse_timestamp(evidence["checked_at"], "availability check")
            previous_at = parse_timestamp(entry.get("links_recorded_at"), "supplier links")
            if checked_at <= previous_at:
                raise ValueError("Check availability again before replacing supplier links")

        recorded_at = now_iso()
        entry["supplier_links"] = urls
        entry["links_recorded_at"] = recorded_at
        entry["status"] = "awaiting_miaoshou_collection"
        entry.pop("pricing_confirmation", None)
        if evidence is not None:
            evidence["recorded_links"] = list(urls)
            evidence["recorded_links_at"] = recorded_at
        link_file = output_dir / config["deliverables"]["supplier_links_file"]
        previous_content = link_file.read_text(encoding="utf-8") if link_file.exists() else None
        try:
            atomic_write_text(
                link_file,
                "\n".join(f"Link {index}: {url}" for index, url in enumerate(urls, start=1)) + "\n",
            )
            atomic_write_json(context["history"], history)
        except Exception:
            if previous_content is None:
                link_file.unlink(missing_ok=True)
            else:
                atomic_write_text(link_file, previous_content)
            raise
    print(json.dumps({"history_file": str(context["history"]), "entry": entry}, ensure_ascii=False, indent=2))
    return 0


def command_record_pricing(args: argparse.Namespace, context: dict) -> int:
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not output_dir.is_dir() or not output_dir.is_relative_to(context["output_root"]):
        raise ValueError("Output directory must exist inside output_root")
    currency = args.currency.strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError("currency must be a supported ISO 4217 code")
    if args.expected_sku_count < 1:
        raise ValueError("expected-sku-count must be positive")
    sales_unit = unicodedata.normalize("NFKC", args.sales_unit).strip()
    if not sales_unit:
        raise ValueError("sales-unit must be a non-empty value from Miaoshou and the supplier document")
    sku_file = Path(args.sku_file).expanduser().resolve()
    data = read_json(sku_file)

    with locked(context):
        history = load_history(context["history"])
        entry = find_entry_by_output(history, output_dir, context)
        if entry.get("status") != "awaiting_miaoshou_collection":
            raise ValueError(f"Product status cannot record pricing: {entry.get('status')}")
        normalized_skus = normalize_pricing_skus(
            data,
            entry,
            context,
            currency=currency,
            sales_unit=sales_unit,
        )
        if len(normalized_skus) != args.expected_sku_count:
            raise ValueError("sku-file row count must equal --expected-sku-count")
        needs_cost = any("supplier_cost" in sku for sku in normalized_skus)
        if needs_cost or context["config"]["availability"]["required"]:
            require_availability_setup(context, force=needs_cost)
        evidence = pricing_evidence_payload(
            entry,
            context,
            currency,
            sales_unit,
            args.expected_sku_count,
            normalized_skus,
        )
        entry["pricing_confirmation"] = evidence
        atomic_write_json(context["history"], history)
    print(
        json.dumps(
            {"history_file": str(context["history"]), "pricing": evidence, "entry": entry},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_mark_collected(args: argparse.Namespace, context: dict) -> int:
    config = context["config"]
    require_availability_setup(context)
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not output_dir.is_dir() or not output_dir.is_relative_to(context["output_root"]):
        raise ValueError("Output directory must exist inside output_root")
    resolved_urls = validate_urls(args.resolved_urls) if args.resolved_urls else []
    if not resolved_urls:
        raise ValueError("At least one resolved URL is required for the final Miaoshou record")
    with locked(context):
        history = load_history(context["history"])
        entry = find_entry_by_output(history, output_dir, context)
        if entry.get("status") != "awaiting_miaoshou_collection":
            raise ValueError(f"Product status cannot be marked collected: {entry.get('status')}")
        recorded_urls = entry.get("supplier_links", [])
        collection = config["collection"]
        if not collection["min_links"] <= len(recorded_urls) <= collection["max_links"]:
            raise ValueError("Recorded supplier link count is outside configured limits")
        if len(resolved_urls) > len(recorded_urls):
            raise ValueError("Resolved URL count cannot exceed supplier link count")
        if config["availability"]["required"]:
            evidence = require_availability(entry, context, require_fresh=True)
            if evidence.get("recorded_links") != recorded_urls or evidence.get(
                "recorded_links_at"
            ) != entry.get("links_recorded_at"):
                raise ValueError("Availability evidence is not bound to the current supplier links")
        parse_timestamp(entry.get("links_recorded_at"), "supplier links")
        require_pricing(entry, context)
        deliverable_result = inspect_deliverables(output_dir, context, history)
        if not deliverable_result["valid"]:
            raise ValueError(
                "Prepared deliverables are incomplete or invalid: "
                f"missing={deliverable_result['missing']}, invalid={deliverable_result['invalid']}"
            )
        entry["resolved_urls"] = resolved_urls
        entry["collected_at"] = now_iso()
        entry["status"] = "miaoshou_collected"
        atomic_write_json(context["history"], history)
    print(json.dumps({"history_file": str(context["history"]), "entry": entry}, ensure_ascii=False, indent=2))
    return 0


def command_history(args: argparse.Namespace, context: dict) -> int:
    history = load_history(context["history"])
    print(json.dumps({"history_file": str(context["history"]), **history}, ensure_ascii=False, indent=2))
    return 0


def command_audit(args: argparse.Namespace, context: dict) -> int:
    history = load_history(context["history"])
    collection_errors = availability_setup_errors(context)
    config_errors = []
    missing_catalogs = [str(path) for path in context["catalogs"] if not path.is_dir()]
    if missing_catalogs:
        config_errors.append("one or more configured catalog roots are missing")
    missing_sources = []
    history_source_errors = []
    for entry in history["products"]:
        try:
            if not source_path_from_entry(entry, context).is_dir():
                missing_sources.append(entry["product_code"])
        except (KeyError, TypeError, ValueError) as exc:
            history_source_errors.append({"product_code": entry.get("product_code"), "error": str(exc)})
    valid = not config_errors and not missing_catalogs and not missing_sources and not history_source_errors
    collection_ready = valid and not collection_errors
    payload = {
        "history_file": str(context["history"]),
        "config_fingerprint": config_fingerprint(context["config"]),
        "valid": valid,
        "collection_ready": collection_ready,
        "config_errors": config_errors,
        "collection_errors": collection_errors,
        "missing_catalog_roots": missing_catalogs,
        "total": len(history["products"]),
        "status_counts": dict(
            sorted(collections.Counter(entry.get("status") for entry in history["products"]).items())
        ),
        "missing_source_product_codes": missing_sources,
        "history_source_errors": history_source_errors,
        "canonical_product_codes": sorted(selected_codes(history)),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if valid else 2


def detect_raster_format(path: Path) -> str | None:
    try:
        from PIL import Image, UnidentifiedImageError
    except ModuleNotFoundError as exc:
        raise OSError(
            "Pillow is required to validate prepared images. Install it with: "
            "python -m pip install Pillow"
        ) from exc
    try:
        with Image.open(path) as image:
            image_format = image.format.casefold() if image.format else None
            image.verify()
            return image_format
    except (OSError, UnidentifiedImageError):
        return None


def inspect_deliverables(output_dir: Path, context: dict, history: dict | None = None) -> dict:
    deliverables = context["config"]["deliverables"]
    files = {
        "copy": output_dir / deliverables["copy_file"],
        "scan_image": output_dir / deliverables["scan_image"],
        "spec_chart": output_dir / deliverables["spec_chart"],
    }
    required = ["copy", "scan_image"]
    if deliverables["spec_chart_required"]:
        required.append("spec_chart")
    missing = [name for name in required if not files[name].is_file()]
    invalid = []

    if files["copy"].is_file():
        try:
            content = files["copy"].read_text(encoding="utf-8")
            title_match = re.search(r"(?m)^Title:[ \t]*(.*?)[ \t]*\r?$", content)
            content_match = re.search(r"(?m)^Content:[ \t]*\r?$", content)
            title = title_match.group(1).strip() if title_match else ""
            content_body = content[content_match.end() :] if content_match else ""
            has_field = any(
                re.match(r"^[^:\r\n]+:[ \t]*\S", line) for line in content_body.splitlines()
            )
            if (
                not title
                or len(title) > context["config"]["copy"]["title_max_chars"]
                or content_match is None
                or not has_field
            ):
                invalid.append("copy")
        except (OSError, UnicodeDecodeError):
            invalid.append("copy")
    if files["scan_image"].is_file() and detect_raster_format(files["scan_image"]) != "jpeg":
        invalid.append("scan_image")
    if files["spec_chart"].is_file() and detect_raster_format(files["spec_chart"]) != "png":
        invalid.append("spec_chart")

    inside_output = output_dir.is_relative_to(context["output_root"])
    recorded = False
    if inside_output:
        history = history if history is not None else load_history(context["history"])
        try:
            find_entry_by_output(history, output_dir, context)
            recorded = True
        except ValueError:
            pass
    return {
        "output_dir": str(output_dir),
        "files": {name: str(path) for name, path in files.items()},
        "missing": missing,
        "invalid": sorted(set(invalid)),
        "inside_output_root": inside_output,
        "recorded_in_history": recorded,
        "valid": not missing and not invalid and inside_output and recorded,
    }


def command_verify(args: argparse.Namespace, context: dict) -> int:
    output_dir = Path(args.output_dir).expanduser().resolve()
    result = inspect_deliverables(output_dir, context)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage permanent state for generic TikTok Shop listings")
    parser.add_argument("--root", default=str(default_root()), help="Workspace root")
    parser.add_argument("--config", help="Config path, relative to workspace unless absolute")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_config = subparsers.add_parser("init-config", help="Create a local config from the bundled example")
    init_config.set_defaults(func=command_init_config)

    show_config = subparsers.add_parser("show-config", help="Print resolved local configuration")
    show_config.set_defaults(func=command_show_config)

    eligible = subparsers.add_parser("eligible", help="List image-containing catalog directories not yet selected")
    eligible.add_argument("--limit", type=int, default=0)
    eligible.set_defaults(func=command_eligible)

    reserve = subparsers.add_parser("reserve", help="Reserve one product permanently and create its output folder")
    reserve.add_argument("source")
    reserve.add_argument("--product-code", required=True)
    reserve.add_argument("--alias", dest="aliases", action="append", default=[])
    reserve.add_argument("--category")
    reserve.add_argument("--tag", dest="tags", action="append", default=[])
    reserve.add_argument("--date", type=validate_date)
    reserve.set_defaults(func=command_reserve)

    reset = subparsers.add_parser("reset", help="Clear permanent history after explicit user instruction")
    reset.add_argument("--confirm", required=True)
    reset.set_defaults(func=command_reset)

    confirm = subparsers.add_parser("confirm-availability", help="Record an all-worksheet exact availability check")
    confirm.add_argument("output_dir")
    confirm.add_argument("--query", required=True)
    confirm.add_argument("--matched-text", required=True)
    confirm.add_argument("--active-sheet", required=True)
    confirm.add_argument("--active-cell", required=True)
    confirm.add_argument("--scope", required=True, choices=["all_sheets"])
    confirm.add_argument("--sold-out-status", required=True, choices=["clear"])
    confirm.set_defaults(func=command_confirm_availability)

    record = subparsers.add_parser("record-links", help="Record sales-ranked supplier links")
    record.add_argument("output_dir")
    record.add_argument("urls", nargs="+")
    record.set_defaults(func=command_record_links)

    pricing = subparsers.add_parser("record-pricing", help="Validate and record evidence-backed SKU prices")
    pricing.add_argument("output_dir")
    pricing.add_argument("--currency", required=True)
    pricing.add_argument("--sales-unit", required=True)
    pricing.add_argument("--expected-sku-count", required=True, type=int)
    pricing.add_argument("--sku-file", required=True)
    pricing.set_defaults(func=command_record_pricing)

    collected = subparsers.add_parser("mark-collected", help="Mark a fully edited and cleaned collection complete")
    collected.add_argument("output_dir")
    collected.add_argument("--resolved-url", dest="resolved_urls", action="append", default=[])
    collected.set_defaults(func=command_mark_collected)

    history = subparsers.add_parser("history", help="Print permanent listing history")
    history.set_defaults(func=command_history)

    audit = subparsers.add_parser("audit", help="Audit history, status counts, config, and source paths")
    audit.set_defaults(func=command_audit)

    verify = subparsers.add_parser("verify", help="Verify prepared deliverables for one product")
    verify.add_argument("output_dir")
    verify.set_defaults(func=command_verify)
    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    config_path = config_path_for(root, args.config)
    try:
        if args.command == "init-config":
            return args.func(args, root, config_path)
        config = load_config(config_path)
        context = build_context(root, config_path, config)
        return args.func(args, context)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
