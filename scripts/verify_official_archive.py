#!/usr/bin/env python3
import argparse
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def digest_records(records: list[str]) -> str:
    return sha256_bytes("\n".join(sorted(records)).encode("utf-8"))


def load_article_parser():
    source = Path(__file__).with_name("wechat_archive.py")
    spec = importlib.util.spec_from_file_location("wechat_archive_verifier_runtime", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load wechat_archive.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WeChatArticleParser, module.VERIFICATION_MARKERS


def verify_archive(root: Path, parent_job_id: str) -> dict:
    root = root.expanduser().resolve()
    jobs_root = root / "jobs"
    parent_path = jobs_root / parent_job_id / "manifest.json"
    parent = read_json(parent_path)
    parent_items = parent.get("items") or []
    if not isinstance(parent_items, list):
        raise ValueError("parent items must be a list")

    children: list[tuple[Path, dict]] = []
    for manifest_path in sorted(jobs_root.glob("content-*/manifest.json")):
        manifest = read_json(manifest_path)
        if str(manifest.get("parent_job_id") or "") == parent_job_id:
            children.append((manifest_path, manifest))

    errors = Counter()
    statuses = Counter(str(manifest.get("status") or "unknown") for _, manifest in children)
    roles = Counter()
    content_ids = Counter(str(manifest.get("content_id") or "") for _, manifest in children)
    canonical_urls = Counter(str(manifest.get("canonical_url") or manifest.get("source") or "") for _, manifest in children)
    output_paths = Counter()
    output_records: list[str] = []
    inventory_records: list[str] = []
    expected_dirs: set[Path] = set()
    verified_files = 0
    verified_bytes = 0
    checksum_failures = 0

    children_by_job = {str(manifest.get("job_id") or ""): manifest for _, manifest in children}
    for item in parent_items:
        if not isinstance(item, dict):
            errors["invalid_parent_item"] += 1
            continue
        child_job_id = str(item.get("child_job_id") or "")
        child = children_by_job.get(child_job_id)
        if child is None:
            errors["missing_child_reference"] += 1
            child_status = "missing"
        else:
            child_status = str(child.get("status") or "unknown")
        content_digest = sha256_bytes(str(item.get("content_id") or "").encode("utf-8"))
        url_digest = sha256_bytes(str(item.get("canonical_url") or "").encode("utf-8"))
        inventory_records.append(f"{content_digest}|{url_digest}|{child_status}")

    for manifest_path, manifest in children:
        status = str(manifest.get("status") or "unknown")
        if status == "completed":
            output_dir = str(manifest.get("output_dir") or "")
            if not output_dir:
                errors["missing_output_dir_record"] += 1
            else:
                candidate = (root / output_dir).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    errors["unsafe_output_dir"] += 1
                else:
                    expected_dirs.add(candidate)
            outputs = manifest.get("outputs") or []
            if not isinstance(outputs, list) or not outputs:
                errors["missing_outputs"] += 1
                continue
            present_roles = {str(item.get("role") or "") for item in outputs if isinstance(item, dict)}
            if not {"original_html", "body_markdown"}.issubset(present_roles):
                errors["missing_required_role"] += 1
            for output in outputs:
                if not isinstance(output, dict):
                    errors["invalid_output_record"] += 1
                    continue
                relative = str(output.get("path") or "")
                role = str(output.get("role") or "unknown")
                output_paths[relative] += 1
                path_digest = sha256_bytes(relative.encode("utf-8"))
                path = (root / relative).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    errors["unsafe_output_path"] += 1
                    continue
                if not path.is_file():
                    errors["missing_output_file"] += 1
                    continue
                actual_size = path.stat().st_size
                actual_sha256 = sha256_file(path)
                recorded_size = int(output.get("bytes") or -1)
                recorded_sha256 = str(output.get("sha256") or "")
                if actual_size != recorded_size:
                    errors["byte_mismatch"] += 1
                if actual_sha256 != recorded_sha256:
                    errors["checksum_mismatch"] += 1
                    checksum_failures += 1
                if role in {"original_html", "body_markdown"} and actual_size <= 0:
                    errors["empty_required_output"] += 1
                roles[role] += 1
                verified_files += 1
                verified_bytes += actual_size
                output_records.append(f"{role}|{actual_size}|{actual_sha256}|{path_digest}")
        elif status != "unavailable":
            errors["nonterminal_or_failed_child"] += 1

    duplicate_content_ids = sum(count - 1 for value, count in content_ids.items() if value and count > 1)
    duplicate_urls = sum(count - 1 for value, count in canonical_urls.items() if value and count > 1)
    duplicate_output_paths = sum(count - 1 for value, count in output_paths.items() if value and count > 1)
    if duplicate_content_ids:
        errors["duplicate_content_id"] += duplicate_content_ids
    if duplicate_urls:
        errors["duplicate_url"] += duplicate_urls
    if duplicate_output_paths:
        errors["duplicate_output_path"] += duplicate_output_paths

    content_root = root / "content" / "公众号"
    actual_dirs = {path.resolve() for path in content_root.iterdir() if path.is_dir()} if content_root.is_dir() else set()
    missing_dirs = len(expected_dirs - actual_dirs)
    extra_dirs = len(actual_dirs - expected_dirs)
    if missing_dirs:
        errors["missing_content_dir"] += missing_dirs
    if extra_dirs:
        errors["extra_content_dir"] += extra_dirs

    Parser, verification_markers = load_article_parser()
    unavailable_review = Counter()
    unavailable_review["total"] = statuses.get("unavailable", 0)
    for manifest_path, manifest in children:
        if manifest.get("status") != "unavailable":
            continue
        original = manifest_path.parent / "original.html"
        if not original.is_file():
            errors["unavailable_original_missing"] += 1
            continue
        parser = Parser()
        parser.feed(original.read_text(encoding="utf-8", errors="replace"))
        body = parser.article_text()
        page_text = parser.page_text()
        hits = sum(marker in f"{parser.title}\n{page_text}" for marker in verification_markers)
        if len(parser.title.strip()) == 0:
            unavailable_review["empty_title"] += 1
        if len(body.strip()) == 0:
            unavailable_review["empty_body"] += 1
        if len(parser.images) == 0:
            unavailable_review["empty_images"] += 1
        if hits == 0:
            unavailable_review["zero_verification_markers"] += 1

    if len(parent_items) != len(children):
        errors["parent_child_count_mismatch"] += 1

    summary = {
        "schema_version": 1,
        "parent_status": str(parent.get("status") or "unknown"),
        "parent_items": len(parent_items),
        "child_jobs": len(children),
        "status_counts": dict(sorted(statuses.items())),
        "output_roles": dict(sorted(roles.items())),
        "verified_output_files": verified_files,
        "verified_output_bytes": verified_bytes,
        "checksum_failures": checksum_failures,
        "duplicate_content_ids": duplicate_content_ids,
        "duplicate_urls": duplicate_urls,
        "duplicate_output_paths": duplicate_output_paths,
        "missing_content_dirs": missing_dirs,
        "extra_content_dirs": extra_dirs,
        "unavailable_secondary_review": dict(sorted(unavailable_review.items())),
        "inventory_root_sha256": digest_records(inventory_records),
        "output_record_root_sha256": digest_records(output_records),
        "error_counts": dict(sorted(errors.items())),
        "hard_errors": sum(errors.values()),
    }
    summary["snapshot_sha256"] = sha256_bytes(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an Official Account batch without disclosing titles, URLs, or paths.")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--parent-job-id", required=True)
    args = parser.parse_args()
    summary = verify_archive(args.root, args.parent_job_id)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if summary["hard_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
