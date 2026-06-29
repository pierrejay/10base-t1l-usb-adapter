#!/usr/bin/env python3
"""
Sync only the EasyEDA 3D models used by a KiCad project.

The script reads model references from a KiCad PCB and local .pretty
footprints, copies the matching files from a central easyeda2kicad library,
and optionally asks easyeda2kicad to fetch missing LCSC parts.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


DEFAULT_LIBRARY = Path("~/Documents/KiCad/easyeda2kicad").expanduser()
MODEL_EXTENSIONS = (".step", ".stp", ".wrl", ".wings")
SUPPLIER_PART_KEYS = ("Supplier Part", "LCSC", "LCSC Part", "LCSC Part Number")


@dataclass(frozen=True)
class ModelRef:
    model_path: str
    source: Path
    footprint: str | None

    @property
    def basename(self) -> str:
        return Path(self.model_path.replace("\\", "/")).name

    @property
    def stem(self) -> str:
        return Path(self.basename).stem


@dataclass(frozen=True)
class TextBlock:
    name: str | None
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class FetchResult:
    ok: bool
    transient: bool
    message: str


@dataclass(frozen=True)
class SwapOutput:
    expected_name: str
    actual_name: str


def die(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def find_matching_paren(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1

    return None


def iter_blocks(text: str, keyword: str):
    for block in iter_text_blocks(text, keyword):
        yield block.name, block.text


def iter_text_blocks(text: str, keyword: str):
    pattern = re.compile(rf"\(\s*{re.escape(keyword)}(?:\s+\"([^\"]+)\")?")
    for match in pattern.finditer(text):
        end = find_matching_paren(text, match.start())
        if end is not None:
            yield TextBlock(match.group(1), match.start(), end, text[match.start() : end])


def model_paths_from_text(text: str) -> list[str]:
    return re.findall(r'\(\s*model\s+"([^"]+)"', text)


def iter_model_blocks(text: str):
    return iter_text_blocks(text, "model")


def rewrite_model_block_path(block: str, prefix: str, basename: str | None = None) -> str:
    match = re.search(r'\(\s*model\s+"([^"]+)"', block)
    if not match:
        return block

    model_path = match.group(1)
    model_name = basename or Path(model_path.replace("\\", "/")).name
    new_head = f'(model "{prefix}/{model_name}"'
    return block[: match.start()] + new_head + block[match.end() :]


def footprint_basename(footprint: str | None) -> str | None:
    if not footprint:
        return None
    return footprint.split(":")[-1]


def discover_project(path: Path) -> tuple[Path, list[Path], list[Path], list[Path]]:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix == ".kicad_pcb":
        project_dir = path.parent
        pcb_files = [path]
    elif path.suffix == ".kicad_pro":
        project_dir = path.parent
        pcb_files = sorted(project_dir.glob("*.kicad_pcb"))
    elif path.is_dir():
        project_dir = path
        pcb_files = sorted(project_dir.glob("*.kicad_pcb"))
    else:
        raise ValueError(f"unsupported project path: {path}")

    pretty_dirs = sorted(project_dir.glob("*.pretty"))
    footprint_files = [fp for pretty in pretty_dirs for fp in sorted(pretty.glob("*.kicad_mod"))]
    symbol_files = sorted(project_dir.glob("*.kicad_sym")) + sorted(project_dir.glob("*.kicad_sch"))

    return project_dir, pcb_files, footprint_files, symbol_files


def collect_model_refs(pcb_files: list[Path], footprint_files: list[Path]) -> list[ModelRef]:
    refs: list[ModelRef] = []

    for pcb in pcb_files:
        text = read_text(pcb)
        for footprint_name, block in iter_blocks(text, "footprint"):
            fp_name = footprint_basename(footprint_name)
            for model_path in model_paths_from_text(block):
                refs.append(ModelRef(model_path, pcb, fp_name))

    for mod in footprint_files:
        text = read_text(mod)
        footprint_name = None
        match = re.search(r'\(\s*footprint\s+"([^"]+)"', text)
        if match:
            footprint_name = footprint_basename(match.group(1))
        else:
            footprint_name = mod.stem

        for model_path in model_paths_from_text(text):
            refs.append(ModelRef(model_path, mod, footprint_name))

    unique: dict[tuple[str, str | None, Path], ModelRef] = {}
    for ref in refs:
        unique[(ref.model_path, ref.footprint, ref.source)] = ref
    return sorted(unique.values(), key=lambda item: (item.basename, str(item.source)))


def collect_used_footprints(pcb_files: list[Path], footprint_files: list[Path]) -> set[str]:
    footprints: set[str] = {fp.stem for fp in footprint_files}
    for pcb in pcb_files:
        text = read_text(pcb)
        for footprint_name, _block in iter_blocks(text, "footprint"):
            name = footprint_basename(footprint_name)
            if name:
                footprints.add(name)
    return footprints


def collect_footprint_lcsc_ids(symbol_files: list[Path]) -> dict[str, list[str]]:
    ids_by_footprint: dict[str, set[str]] = defaultdict(set)

    for symbol_file in symbol_files:
        text = read_text(symbol_file)
        for _name, block in iter_blocks(text, "symbol"):
            properties = dict(re.findall(r'\(\s*property\s+"([^"]+)"\s+"([^"]*)"', block))
            footprint = footprint_basename(properties.get("Footprint"))
            if not footprint:
                continue

            lcsc_ids: set[str] = set()
            for key in SUPPLIER_PART_KEYS:
                value = properties.get(key)
                if value and re.fullmatch(r"C\d+", value.strip()):
                    lcsc_ids.add(value.strip())

            for value in properties.values():
                for lcsc_id in re.findall(r"\bC\d{2,}\b", value):
                    lcsc_ids.add(lcsc_id)

            ids_by_footprint[footprint].update(lcsc_ids)

    return {key: sorted(value, key=lambda item: int(item[1:])) for key, value in ids_by_footprint.items()}


def invert_footprint_lcsc_ids(ids_by_footprint: dict[str, list[str]]) -> dict[str, list[str]]:
    footprints_by_id: dict[str, list[str]] = defaultdict(list)
    for footprint, lcsc_ids in ids_by_footprint.items():
        for lcsc_id in lcsc_ids:
            if footprint not in footprints_by_id[lcsc_id]:
                footprints_by_id[lcsc_id].append(footprint)
    return dict(footprints_by_id)


def collect_footprint_lcsc_ids_from_mods(footprint_files: list[Path]) -> dict[str, list[str]]:
    ids_by_footprint: dict[str, set[str]] = defaultdict(set)

    for footprint_file in footprint_files:
        text = read_text(footprint_file)
        footprint_name = footprint_file.stem
        match = re.search(r'\(\s*(?:footprint|module)\s+"?([^"\s)]+)"?', text)
        if match:
            footprint_name = footprint_basename(match.group(1)) or footprint_name

        for _key, value in re.findall(r'\(\s*property\s+"([^"]+)"\s+"([^"]*)"', text):
            for lcsc_id in re.findall(r"\bC\d{2,}\b", value):
                ids_by_footprint[footprint_name].add(lcsc_id)

    return {key: sorted(value, key=lambda item: int(item[1:])) for key, value in ids_by_footprint.items()}


def merge_footprint_lcsc_ids(*maps: dict[str, list[str]]) -> dict[str, list[str]]:
    merged: dict[str, set[str]] = defaultdict(set)
    for mapping in maps:
        for footprint, lcsc_ids in mapping.items():
            merged[footprint].update(lcsc_ids)
    return {key: sorted(value, key=lambda item: int(item[1:])) for key, value in merged.items()}


def build_model_index(library_dir: Path) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    shapes_dir = library_dir / "easyeda2kicad.3dshapes"
    if not shapes_dir.exists():
        return {}, {}

    by_name: dict[str, Path] = {}
    by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in shapes_dir.iterdir():
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS:
            by_name.setdefault(path.name, path)
            by_stem[path.stem].append(path)

    for paths in by_stem.values():
        paths.sort(key=lambda item: item.suffix)
    return by_name, dict(by_stem)


def target_path_for_model(project_dir: Path, models_dir_name: str, model_path: str) -> Path:
    normalized = model_path.replace("\\", "/")
    basename = Path(normalized).name

    if normalized.startswith("${KIPRJMOD}/"):
        rel = normalized.removeprefix("${KIPRJMOD}/")
        return project_dir / rel

    return project_dir / models_dir_name / basename


def copy_model(
    source: Path,
    target: Path,
    dry_run: bool,
    overwrite: bool,
    label: str,
) -> bool:
    if target.exists() and not overwrite:
        return False

    print(f"{label}: {source.name} -> {target}")
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return True


def sync_from_library(
    refs: list[ModelRef],
    project_dir: Path,
    library_dir: Path,
    models_dir_name: str,
    dry_run: bool,
    overwrite: bool,
    copy_siblings: bool,
) -> tuple[int, list[ModelRef]]:
    by_name, by_stem = build_model_index(library_dir)
    copied = 0
    missing: list[ModelRef] = []
    seen_targets: set[Path] = set()

    for ref in refs:
        target = target_path_for_model(project_dir, models_dir_name, ref.model_path)
        if target in seen_targets:
            continue
        seen_targets.add(target)

        if target.exists() and not overwrite:
            continue

        source = by_name.get(ref.basename)
        if source:
            copied += int(copy_model(source, target, dry_run, overwrite, "copy"))

            if copy_siblings:
                for sibling in by_stem.get(ref.stem, []):
                    sibling_target = target.with_name(sibling.name)
                    if sibling_target != target:
                        copied += int(copy_model(sibling, sibling_target, dry_run, overwrite, "copy sibling"))
            continue

        missing.append(ref)

    return copied, missing


def candidates_for_missing(
    missing: list[ModelRef],
    ids_by_footprint: dict[str, list[str]],
    used_footprints: set[str],
    max_candidates: int,
) -> dict[str, list[str]]:
    candidates: dict[str, list[str]] = {}

    for ref in missing:
        names = [ref.footprint] if ref.footprint else []
        names += [name for name in used_footprints if name and name in ref.stem]

        ids: list[str] = []
        for name in names:
            for lcsc_id in ids_by_footprint.get(name, []):
                if lcsc_id not in ids:
                    ids.append(lcsc_id)

        candidates[ref.basename] = ids[:max_candidates]

    return candidates


def run_easyeda2kicad(
    library_dir: Path,
    lcsc_id: str,
    overwrite: bool,
    timeout: int,
    dry_run: bool,
    verbose: bool,
) -> FetchResult:
    command = ["easyeda2kicad", "--full", f"--lcsc_id={lcsc_id}"]
    if overwrite:
        command.append("--overwrite")

    print(f"fetch: {' '.join(command)}  (cwd={library_dir})", flush=True)
    if dry_run:
        return FetchResult(True, False, "dry-run")

    try:
        result = subprocess.run(
            command,
            cwd=library_dir,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return FetchResult(False, False, "easyeda2kicad not found in PATH")
    except subprocess.TimeoutExpired:
        return FetchResult(False, True, f"timeout after {timeout}s")

    if result.returncode != 0:
        output = "\n".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)
        message, transient = summarize_fetch_error(output)
        if verbose and output:
            print(output, file=sys.stderr)
        else:
            print(f"  failed: {message}", file=sys.stderr)
        return FetchResult(False, transient, message)

    return FetchResult(True, False, "ok")


def summarize_fetch_error(output: str) -> tuple[str, bool]:
    if not output:
        return "easyeda2kicad failed without output", False

    if "JSONDecodeError" in output or "Expecting value: line 1 column 1" in output:
        return (
            "EasyEDA/JLCPCB API returned a non-JSON response; likely temporary outage, rate-limit, or network issue",
            True,
        )

    transient_patterns = (
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "Temporary failure",
        "NameResolutionError",
        "Max retries exceeded",
        "SSLError",
        "Too Many Requests",
        "429",
        "502",
        "503",
        "504",
    )
    if any(pattern in output for pattern in transient_patterns):
        return ("temporary network/API failure while fetching from EasyEDA/JLCPCB", True)

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    interesting = [line for line in lines if "Error" in line or "Exception" in line or "failed" in line.lower()]
    if interesting:
        return (interesting[-1], False)
    return (lines[-1] if lines else "easyeda2kicad failed", False)


def fetch_and_resync(
    missing: list[ModelRef],
    project_dir: Path,
    library_dir: Path,
    models_dir_name: str,
    ids_by_footprint: dict[str, list[str]],
    used_footprints: set[str],
    dry_run: bool,
    local_overwrite: bool,
    fetch_overwrite: bool,
    copy_siblings: bool,
    max_candidates: int,
    timeout: int,
    delay: float,
    max_fetch_failures: int,
    fetch_verbose: bool,
) -> tuple[int, list[ModelRef]]:
    candidates = candidates_for_missing(missing, ids_by_footprint, used_footprints, max_candidates)
    fetched_ids: set[str] = set()
    unresolved = list(missing)
    copied_total = 0
    consecutive_fetch_failures = 0

    for ref in missing:
        target = target_path_for_model(project_dir, models_dir_name, ref.model_path)
        if target.exists() and not local_overwrite:
            continue

        for lcsc_id in candidates.get(ref.basename, []):
            if lcsc_id not in fetched_ids:
                result = run_easyeda2kicad(
                    library_dir,
                    lcsc_id,
                    fetch_overwrite,
                    timeout,
                    dry_run,
                    fetch_verbose,
                )
                if not result.ok:
                    consecutive_fetch_failures += 1
                    if max_fetch_failures > 0 and consecutive_fetch_failures >= max_fetch_failures:
                        print(
                            f"fetch aborted after {consecutive_fetch_failures} consecutive failures; "
                            "rerun later or use --no-fetch to only copy cached models",
                            file=sys.stderr,
                        )
                        return copied_total, unresolved
                    continue
                consecutive_fetch_failures = 0
                fetched_ids.add(lcsc_id)
                if delay > 0 and not dry_run:
                    time.sleep(delay)

            copied, still_missing = sync_from_library(
                [ref],
                project_dir,
                library_dir,
                models_dir_name,
                dry_run,
                local_overwrite,
                copy_siblings,
            )
            copied_total += copied
            if not still_missing:
                if ref in unresolved:
                    unresolved.remove(ref)
                break

    return copied_total, unresolved


def expected_refs_for_lcsc(
    lcsc_id: str,
    refs: list[ModelRef],
    ids_by_footprint: dict[str, list[str]],
) -> list[ModelRef]:
    footprints = set(invert_footprint_lcsc_ids(ids_by_footprint).get(lcsc_id, []))
    expected = [ref for ref in refs if ref.footprint in footprints]

    unique: dict[str, ModelRef] = {}
    for ref in expected:
        unique.setdefault(ref.basename, ref)
    return sorted(unique.values(), key=lambda ref: ref.basename)


def central_footprints_for_lcsc(library_dir: Path, lcsc_id: str) -> list[str]:
    central_symbols = [library_dir / "easyeda2kicad.kicad_sym"]
    central_pretty = library_dir / "easyeda2kicad.pretty"
    central_mods = sorted(central_pretty.glob("*.kicad_mod")) if central_pretty.exists() else []

    ids_by_footprint = merge_footprint_lcsc_ids(
        collect_footprint_lcsc_ids([path for path in central_symbols if path.exists()]),
        collect_footprint_lcsc_ids_from_mods(central_mods),
    )
    return sorted(invert_footprint_lcsc_ids(ids_by_footprint).get(lcsc_id, []))


def model_files_for_footprints(library_dir: Path, footprints: list[str]) -> list[Path]:
    shapes_by_name, _by_stem = build_model_index(library_dir)
    model_files: list[Path] = []

    for footprint in footprints:
        footprint_file = library_dir / "easyeda2kicad.pretty" / f"{footprint}.kicad_mod"
        if not footprint_file.exists():
            continue

        for model_path in model_paths_from_text(read_text(footprint_file)):
            model_name = Path(model_path.replace("\\", "/")).name
            model_file = shapes_by_name.get(model_name)
            if model_file and model_file not in model_files:
                model_files.append(model_file)

    return sorted(model_files, key=model_sort_key)


def model_sort_key(path: Path) -> tuple[int, str]:
    suffix_rank = {".step": 0, ".stp": 1, ".wrl": 2, ".wings": 3}
    return (suffix_rank.get(path.suffix.lower(), 9), path.name)


def choose_source_model(expected_name: str, source_models: list[Path]) -> Path | None:
    expected_suffix = Path(expected_name).suffix.lower()
    same_suffix = [path for path in source_models if path.suffix.lower() == expected_suffix]
    if same_suffix:
        return sorted(same_suffix, key=model_sort_key)[0]
    return sorted(source_models, key=model_sort_key)[0] if source_models else None


def copy_swapped_model_pair(
    source: Path,
    expected_name: str,
    library_dir: Path,
    project_dir: Path,
    models_dir_name: str,
    dry_run: bool,
    overwrite: bool,
) -> tuple[int, list[SwapOutput]]:
    copied = 0
    outputs: list[SwapOutput] = []
    expected = Path(expected_name)
    source_stem = source.stem
    expected_stem = expected.stem
    shapes_dir = library_dir / "easyeda2kicad.3dshapes"

    source_siblings = sorted(
        [
            path
            for path in source.parent.iterdir()
            if path.is_file() and path.stem == source_stem and path.suffix.lower() in MODEL_EXTENSIONS
        ],
        key=model_sort_key,
    )

    for sibling in source_siblings:
        target_name = f"{expected_stem}{sibling.suffix}"
        central_target = shapes_dir / target_name
        project_target = project_dir / models_dir_name / target_name
        copied += int(copy_model(sibling, central_target, dry_run, overwrite, "swap library"))
        copied += int(copy_model(sibling, project_target, dry_run, overwrite, "swap project"))
        outputs.append(SwapOutput(expected_name, target_name))

    return copied, outputs


def rewrite_swapped_model_refs(
    paths: list[Path],
    models_dir_name: str,
    swaps: list[SwapOutput],
    dry_run: bool,
) -> int:
    outputs_by_expected: dict[str, list[str]] = defaultdict(list)
    for swap in swaps:
        outputs_by_expected[swap.expected_name].append(swap.actual_name)

    rewrites: dict[str, str] = {}
    for expected_name, actual_names in outputs_by_expected.items():
        expected_suffix = Path(expected_name).suffix.lower()
        if any(Path(actual_name).suffix.lower() == expected_suffix for actual_name in actual_names):
            continue
        rewrites[expected_name] = sorted((Path(name) for name in actual_names), key=model_sort_key)[0].name

    if not rewrites:
        return 0

    changed = 0
    for path in paths:
        text = read_text(path)

        def repl(match: re.Match[str]) -> str:
            model_path = match.group(1)
            basename = Path(model_path.replace("\\", "/")).name
            replacement = rewrites.get(basename)
            if not replacement:
                return match.group(0)
            return f'(model "${{KIPRJMOD}}/{models_dir_name}/{replacement}"'

        new_text = re.sub(r'\(model\s+"([^"]+)"', repl, text)
        if new_text == text:
            continue

        changed += 1
        print(f"swap rewrite: {path}")
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")

    return changed


def swap_footprint_model(
    original_lcsc: str,
    alternate_lcsc: str,
    refs: list[ModelRef],
    ids_by_footprint: dict[str, list[str]],
    project_dir: Path,
    library_dir: Path,
    models_dir_name: str,
    dry_run: bool,
    overwrite: bool,
    allow_fetch: bool,
    fetch_overwrite: bool,
    timeout: int,
    fetch_verbose: bool,
) -> tuple[int, list[str], list[SwapOutput]]:
    expected_refs = expected_refs_for_lcsc(original_lcsc, refs, ids_by_footprint)
    if not expected_refs:
        print(f"swap: no model references found in this project for {original_lcsc}", file=sys.stderr)
        return 0, [], []

    alternate_footprints = central_footprints_for_lcsc(library_dir, alternate_lcsc)
    source_models = model_files_for_footprints(library_dir, alternate_footprints)

    if not source_models:
        if not allow_fetch:
            print(
                f"swap: alternate {alternate_lcsc} is not cached and --no-fetch is set",
                file=sys.stderr,
            )
            return 0, [ref.basename for ref in expected_refs], []

        before = set((library_dir / "easyeda2kicad.3dshapes").glob("*"))
        result = run_easyeda2kicad(
            library_dir,
            alternate_lcsc,
            fetch_overwrite,
            timeout,
            dry_run,
            fetch_verbose,
        )
        if not result.ok:
            print(f"swap: fetch of alternate {alternate_lcsc} failed: {result.message}", file=sys.stderr)
            return 0, [ref.basename for ref in expected_refs], []

        alternate_footprints = central_footprints_for_lcsc(library_dir, alternate_lcsc)
        source_models = model_files_for_footprints(library_dir, alternate_footprints)

        if not source_models and not dry_run:
            after = set((library_dir / "easyeda2kicad.3dshapes").glob("*"))
            source_models = sorted(
                [path for path in after - before if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS],
                key=model_sort_key,
            )

    if not source_models:
        print(f"swap: no source 3D model found for alternate {alternate_lcsc}", file=sys.stderr)
        return 0, [ref.basename for ref in expected_refs], []

    print(
        "swap: "
        f"{original_lcsc} ({', '.join(ref.basename for ref in expected_refs)}) "
        f"<- {alternate_lcsc} ({', '.join(path.name for path in source_models)})"
    )
    if original_lcsc == alternate_lcsc:
        print("swap: same LCSC on both sides; normalizing the cached 3D model filename to match this project")

    copied = 0
    unresolved: list[str] = []
    outputs: list[SwapOutput] = []
    for ref in expected_refs:
        source = choose_source_model(ref.basename, source_models)
        if not source:
            print(f"swap: no source model available for {ref.basename}", file=sys.stderr)
            unresolved.append(ref.basename)
            continue

        copied_now, outputs_now = copy_swapped_model_pair(
            source,
            ref.basename,
            library_dir,
            project_dir,
            models_dir_name,
            dry_run,
            overwrite,
        )
        copied += copied_now
        outputs.extend(outputs_now)

        actual_suffixes = {Path(output.actual_name).suffix.lower() for output in outputs_now}
        expected_suffix = Path(ref.basename).suffix.lower()
        if expected_suffix not in actual_suffixes and actual_suffixes:
            print(
                f"swap: {ref.basename} will use {expected_stem_with_suffix(ref.basename, sorted(actual_suffixes)[0])}; "
                "project refs need rewrite",
            )

    return copied, unresolved, outputs


def expected_stem_with_suffix(expected_name: str, suffix: str) -> str:
    return f"{Path(expected_name).stem}{suffix}"


def rewrite_model_paths(
    paths: list[Path],
    models_dir_name: str,
    dry_run: bool,
) -> int:
    changed = 0

    for path in paths:
        text = read_text(path)

        def repl(match: re.Match[str]) -> str:
            model_path = match.group(1)
            basename = Path(model_path.replace("\\", "/")).name
            return f'(model "${{KIPRJMOD}}/{models_dir_name}/{basename}"'

        new_text = re.sub(r'\(model\s+"([^"]+)"', repl, text)
        if new_text == text:
            continue

        changed += 1
        print(f"rewrite: {path}")
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")

    return changed


def replace_model_blocks(
    text: str,
    source_blocks: list[str],
    prefix: str,
) -> str:
    target_blocks = list(iter_model_blocks(text))
    if not source_blocks:
        return text

    if not target_blocks:
        insert_at = text.rstrip().rfind(")")
        if insert_at < 0:
            return text

        trailing = text[insert_at:]
        before = text[:insert_at].rstrip()
        replacement = "\n".join(
            f"  {rewrite_model_block_path(block, prefix)}" for block in source_blocks
        )
        return f"{before}\n{replacement}\n{trailing}"

    new_text = text
    for index, target in reversed(list(enumerate(target_blocks))):
        source = source_blocks[min(index, len(source_blocks) - 1)]
        replacement = rewrite_model_block_path(source, prefix)
        new_text = new_text[: target.start] + replacement + new_text[target.end :]

    return new_text


def model_blocks_by_footprint_from_files(footprint_files: list[Path]) -> dict[str, list[str]]:
    blocks_by_footprint: dict[str, list[str]] = {}

    for footprint_file in footprint_files:
        text = read_text(footprint_file)
        footprint_name = footprint_file.stem
        match = re.search(r'\(\s*footprint\s+"([^"]+)"', text)
        if match:
            footprint_name = footprint_basename(match.group(1)) or footprint_name

        blocks = [block.text for block in iter_model_blocks(text)]
        if blocks:
            blocks_by_footprint[footprint_name] = blocks

    return blocks_by_footprint


def model_blocks_by_footprint_from_pcbs(pcb_files: list[Path]) -> dict[str, list[str]]:
    blocks_by_footprint: dict[str, list[str]] = {}

    for pcb_file in pcb_files:
        text = read_text(pcb_file)
        for footprint_block in iter_text_blocks(text, "footprint"):
            footprint_name = footprint_basename(footprint_block.name)
            if not footprint_name or footprint_name in blocks_by_footprint:
                continue

            blocks = [block.text for block in iter_model_blocks(footprint_block.text)]
            if blocks:
                blocks_by_footprint[footprint_name] = blocks

    return blocks_by_footprint


def project_model_file(project_dir: Path, models_dir_name: str, model_path: str) -> Path:
    normalized = model_path.replace("\\", "/")
    basename = Path(normalized).name

    if normalized.startswith("${KIPRJMOD}/"):
        return project_dir / normalized.removeprefix("${KIPRJMOD}/")
    if normalized.startswith("${EASYEDA2KICAD}/"):
        return project_dir / models_dir_name / basename
    if Path(normalized).is_absolute():
        return Path(normalized)
    return project_dir / normalized


def copy_project_models_to_library(
    refs: list[ModelRef],
    project_dir: Path,
    library_dir: Path,
    models_dir_name: str,
    dry_run: bool,
    overwrite: bool,
) -> int:
    copied = 0
    shapes_dir = library_dir / "easyeda2kicad.3dshapes"
    seen: set[Path] = set()

    for ref in refs:
        source = project_model_file(project_dir, models_dir_name, ref.model_path)
        if source in seen or not source.exists():
            continue
        seen.add(source)

        target = shapes_dir / source.name
        copied += int(copy_model(source, target, dry_run, overwrite, "push model"))

    return copied


def push_footprint_model_blocks_to_library(
    pcb_files: list[Path],
    footprint_files: list[Path],
    library_dir: Path,
    dry_run: bool,
) -> int:
    local_blocks = model_blocks_by_footprint_from_files(footprint_files)
    # KiCad's 3D viewer edits are often stored on PCB footprint instances.
    # Prefer those per-board corrections when publishing back to the library.
    local_blocks.update(model_blocks_by_footprint_from_pcbs(pcb_files))

    central_pretty = library_dir / "easyeda2kicad.pretty"
    changed = 0

    for footprint_name, source_blocks in sorted(local_blocks.items()):
        central_file = central_pretty / f"{footprint_name}.kicad_mod"
        if not central_file.exists():
            continue

        text = read_text(central_file)
        new_text = replace_model_blocks(
            text,
            source_blocks,
            "${EASYEDA2KICAD}/easyeda2kicad.3dshapes",
        )
        if new_text == text:
            continue

        changed += 1
        print(f"push footprint: {central_file}")
        if not dry_run:
            central_file.write_text(new_text, encoding="utf-8")

    return changed


def pull_footprint_model_blocks_from_library(
    pcb_files: list[Path],
    footprint_files: list[Path],
    library_dir: Path,
    models_dir_name: str,
    dry_run: bool,
) -> tuple[int, int]:
    central_blocks = model_blocks_by_footprint_from_files(
        sorted((library_dir / "easyeda2kicad.pretty").glob("*.kicad_mod"))
    )
    footprint_changed = 0
    pcb_changed = 0
    project_prefix = f"${{KIPRJMOD}}/{models_dir_name}"

    for footprint_file in footprint_files:
        source_blocks = central_blocks.get(footprint_file.stem)
        if not source_blocks:
            continue

        text = read_text(footprint_file)
        new_text = replace_model_blocks(text, source_blocks, project_prefix)
        if new_text == text:
            continue

        footprint_changed += 1
        print(f"pull footprint: {footprint_file}")
        if not dry_run:
            footprint_file.write_text(new_text, encoding="utf-8")

    for pcb_file in pcb_files:
        text = read_text(pcb_file)
        new_text = text

        for footprint_block in reversed(list(iter_text_blocks(text, "footprint"))):
            footprint_name = footprint_basename(footprint_block.name)
            if not footprint_name:
                continue

            source_blocks = central_blocks.get(footprint_name)
            if not source_blocks:
                continue

            replacement = replace_model_blocks(footprint_block.text, source_blocks, project_prefix)
            if replacement != footprint_block.text:
                new_text = new_text[: footprint_block.start] + replacement + new_text[footprint_block.end :]

        if new_text == text:
            continue

        pcb_changed += 1
        print(f"pull pcb: {pcb_file}")
        if not dry_run:
            pcb_file.write_text(new_text, encoding="utf-8")

    return footprint_changed, pcb_changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy/fetch the EasyEDA 3D models required by a KiCad project."
    )
    parser.add_argument(
        "project",
        nargs="?",
        default=".",
        help="KiCad project directory, .kicad_pro, or .kicad_pcb (default: current directory)",
    )
    parser.add_argument(
        "--library",
        default=str(DEFAULT_LIBRARY),
        help=f"central easyeda2kicad library (default: {DEFAULT_LIBRARY})",
    )
    parser.add_argument(
        "--models-dir",
        default="EASYEDA_MODELS",
        help="project-local models directory name (default: EASYEDA_MODELS)",
    )
    parser.add_argument("--dry-run", action="store_true", help="show actions without writing files")
    parser.add_argument("--overwrite", action="store_true", help="overwrite existing model files")
    parser.add_argument("--no-siblings", action="store_true", help="copy only the exact referenced model")
    parser.add_argument("--no-fetch", action="store_true", help="do not run easyeda2kicad for missing models")
    parser.add_argument(
        "--fetch-overwrite",
        action="store_true",
        help="pass --overwrite to easyeda2kicad when fetching missing parts",
    )
    parser.add_argument("--max-candidates", type=int, default=4, help="max LCSC IDs to try per missing model")
    parser.add_argument("--fetch-timeout", type=int, default=90, help="timeout per easyeda2kicad call in seconds")
    parser.add_argument(
        "--max-fetch-failures",
        type=int,
        default=3,
        help="stop fetching after this many consecutive easyeda2kicad failures; 0 disables the limit",
    )
    parser.add_argument(
        "--fetch-verbose",
        action="store_true",
        help="print the full easyeda2kicad traceback/output when a fetch fails",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="delay between easyeda2kicad calls")
    parser.add_argument(
        "--rewrite-model-paths",
        action="store_true",
        help="rewrite model refs in local PCB/footprints to ${KIPRJMOD}/EASYEDA_MODELS/<file>",
    )
    parser.add_argument(
        "--pull-footprint-models",
        action="store_true",
        help="apply model path/offset/scale/rotate blocks from the central footprints to this project",
    )
    parser.add_argument(
        "--push-to-library",
        action="store_true",
        help="push project model files and footprint model blocks back to the central easyeda2kicad library",
    )
    parser.add_argument(
        "--push-overwrite-models",
        action="store_true",
        help="when pushing, replace existing STEP/WRL files in easyeda2kicad.3dshapes; alignment fixes do not need this",
    )
    parser.add_argument(
        "--swap-footprint",
        nargs=2,
        action="append",
        metavar=("ORIGINAL_LCSC", "ALTERNATE_LCSC"),
        help="fill the original component's expected 3D model by fetching/copying a compatible alternate LCSC model",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        project_dir, pcb_files, footprint_files, symbol_files = discover_project(Path(args.project))
    except (FileNotFoundError, ValueError) as exc:
        return die(str(exc))

    library_dir = Path(args.library).expanduser().resolve()
    if not library_dir.exists():
        return die(f"central library not found: {library_dir}")

    print(f"project: {project_dir}")
    print(f"central library: {library_dir}")
    if args.dry_run:
        print("mode: dry-run")

    pulled_footprints = 0
    pulled_pcbs = 0
    if args.pull_footprint_models:
        pulled_footprints, pulled_pcbs = pull_footprint_model_blocks_from_library(
            pcb_files,
            footprint_files,
            library_dir,
            args.models_dir,
            args.dry_run,
        )

    refs = collect_model_refs(pcb_files, footprint_files)
    if not refs:
        return die("no 3D model references found in the project PCB or local footprints")

    used_footprints = collect_used_footprints(pcb_files, footprint_files)
    ids_by_footprint = merge_footprint_lcsc_ids(
        collect_footprint_lcsc_ids(symbol_files),
        collect_footprint_lcsc_ids_from_mods(footprint_files),
    )

    print(f"model refs: {len({ref.basename for ref in refs})} unique files")
    print(f"footprints: {len(used_footprints)} discovered")
    print(f"LCSC mappings: {sum(1 for key in used_footprints if key in ids_by_footprint)} footprints")

    copied = 0
    unresolved: list[ModelRef] = []
    swap_copied = 0
    swap_unresolved: list[str] = []
    swap_outputs: list[SwapOutput] = []

    for original_lcsc, alternate_lcsc in args.swap_footprint or []:
        copied_now, unresolved_now, outputs_now = swap_footprint_model(
            original_lcsc,
            alternate_lcsc,
            refs,
            ids_by_footprint,
            project_dir,
            library_dir,
            args.models_dir,
            args.dry_run,
            args.overwrite,
            not args.no_fetch,
            args.fetch_overwrite,
            args.fetch_timeout,
            args.fetch_verbose,
        )
        swap_copied += copied_now
        swap_unresolved.extend(unresolved_now)
        swap_outputs.extend(outputs_now)

    swap_rewritten = 0
    if swap_outputs:
        swap_rewritten = rewrite_swapped_model_refs(
            pcb_files + footprint_files,
            args.models_dir,
            swap_outputs,
            args.dry_run,
        )
        refs = collect_model_refs(pcb_files, footprint_files)

    if args.push_to_library:
        print("project sync: skipped for --push-to-library")
    else:
        copied, missing = sync_from_library(
            refs,
            project_dir,
            library_dir,
            args.models_dir,
            args.dry_run,
            args.overwrite,
            not args.no_siblings,
        )

        unresolved = missing
        if missing and not args.no_fetch:
            fetched_copied, unresolved = fetch_and_resync(
                missing,
                project_dir,
                library_dir,
                args.models_dir,
                ids_by_footprint,
                used_footprints,
                args.dry_run,
                args.overwrite,
                args.fetch_overwrite,
                not args.no_siblings,
                args.max_candidates,
                args.fetch_timeout,
                args.delay,
                args.max_fetch_failures,
                args.fetch_verbose,
            )
            copied += fetched_copied

    copied += swap_copied

    rewritten = 0
    if args.rewrite_model_paths:
        rewritten = rewrite_model_paths(pcb_files + footprint_files, args.models_dir, args.dry_run)

    pushed_models = 0
    pushed_footprints = 0
    if args.push_to_library:
        pushed_models = copy_project_models_to_library(
            refs,
            project_dir,
            library_dir,
            args.models_dir,
            args.dry_run,
            args.push_overwrite_models,
        )
        pushed_footprints = push_footprint_model_blocks_to_library(
            pcb_files,
            footprint_files,
            library_dir,
            args.dry_run,
        )

    print()
    print(f"done: {copied} files {'would be ' if args.dry_run else ''}copied")
    if rewritten:
        print(f"model paths {'would be ' if args.dry_run else ''}rewritten in {rewritten} files")
    if pulled_footprints or pulled_pcbs:
        print(
            "central model blocks "
            f"{'would be ' if args.dry_run else ''}pulled into "
            f"{pulled_footprints} footprint files and {pulled_pcbs} PCB files"
        )
    if pushed_models or pushed_footprints:
        print(
            "project corrections "
            f"{'would be ' if args.dry_run else ''}pushed as "
            f"{pushed_models} model files and {pushed_footprints} footprint files"
        )
    if swap_copied:
        print(f"swapped models {'would be ' if args.dry_run else ''}created/copied as {swap_copied} files")
    if swap_rewritten:
        print(f"swapped model refs {'would be ' if args.dry_run else ''}rewritten in {swap_rewritten} files")
    if swap_unresolved:
        print(f"swap unresolved models: {len(set(swap_unresolved))}")
        for name in sorted(set(swap_unresolved)):
            print(f"  {name}")

    if unresolved:
        if args.dry_run and not args.no_fetch:
            print("unresolved models after simulated fetch:")
        else:
            print(f"unresolved models: {len({ref.basename for ref in unresolved})}")
        for ref in sorted(unresolved, key=lambda item: item.basename):
            fp = f" footprint={ref.footprint}" if ref.footprint else ""
            print(f"  {ref.basename}{fp} source={ref.source}")
        return 0 if args.dry_run or args.pull_footprint_models or args.push_to_library else 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
