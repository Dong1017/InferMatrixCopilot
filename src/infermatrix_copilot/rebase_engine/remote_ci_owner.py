"""Remote CI debug ownership; never defines runnable jobs or pass criteria.

Keep existing local routing. Missing remote labels use positive declared path
footprints, preferring specific matches over explicit shared test-root ownership.
Ancillary source imports never imply responsibility or causal attribution.
Unresolved routes remain available for the existing unscoped debug dispatch,
with explicit diagnostics. Provider job IDs and leaf/source identity are the
caller's separate evidence contract; a label is not a unique CI leaf ID.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
import re
import shlex
from typing import Mapping, Sequence

import yaml

from .test_manifest import ManifestSpec, _extract_steps, _label_to_slug


@dataclass(frozen=True)
class RemoteOwner:
    module: str = ""
    status: str = "unmapped"
    reason: str = ""
    candidate_modules: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()


class RemoteOwnerResolver:
    """Read-only, routing-only view; the native local manifest is unchanged."""

    def __init__(self, repo: Path, manifest: Mapping,
                 active_modules: Sequence[str]):
        self.spec = None
        try:
            if not isinstance(manifest, Mapping):
                raise TypeError("invalid_manifest")
            modules = manifest.get("modules")
            modules = {} if modules is None else modules
            rb = manifest.get("rebase")
            rb = {} if rb is None else rb
            if not isinstance(modules, Mapping) or not isinstance(rb, Mapping):
                raise TypeError("invalid_manifest_mapping")
            tm = rb.get("test_manifest")
            tm = {} if tm is None else tm
            if not isinstance(tm, Mapping):
                raise TypeError("invalid_manifest_mapping")
            if isinstance(active_modules, (str, bytes)) or not isinstance(active_modules, Sequence):
                raise TypeError("invalid_active_modules")
            if not all(isinstance(m, str) for m in active_modules):
                raise TypeError("invalid_active_modules")
            for key in ("file_ref_prefixes", "file_tree_roots"):
                if key in tm:
                    self._strings(tm[key])
            assignment = tm.get("assignment_paths")
            assignment = {} if assignment is None else assignment
            if not isinstance(assignment, Mapping):
                raise TypeError("invalid_assignment_paths")
            for module, patterns in assignment.items():
                if not isinstance(module, str):
                    raise TypeError("invalid_module_name")
                self._strings(patterns)
            for module, declared in modules.items():
                if not isinstance(module, str) or (declared is not None and not isinstance(declared, Mapping)):
                    raise TypeError("invalid_module_mapping")
                for key in ("test_paths", "local_paths"):
                    self._strings((declared or {}).get(key))
            spec = replace(ManifestSpec.from_manifest(manifest), skip_patterns=())
            if not isinstance(spec.yaml_dir, str) or not spec.yaml_dir:
                raise TypeError("invalid_yaml_directory")
            if not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in spec.pipelines.items()):
                raise TypeError("invalid_pipeline_mapping")
            self.repo = Path(repo).resolve()
            self.active = tuple(m for m in active_modules if m in modules)
            self.universal = {p.rstrip("/") for p in spec.file_tree_roots}
            self.patterns = {}
            for module in self.active:
                declared = modules[module] or {}
                patterns = [*(spec.assignment_paths.get(module) or ()),
                            *(declared.get("test_paths") or ()),
                            *(declared.get("local_paths") or ())]
                self.patterns[module] = tuple(p for p in dict.fromkeys(patterns) if p)
            roots = {*(spec.file_ref_prefixes or ()),
                     *(PurePosixPath(p).parts[0] for patterns in self.patterns.values()
                       for p in patterns if PurePosixPath(p).parts)}
            roots = {p for p in roots if re.fullmatch(r"[A-Za-z_][\w-]*", p)}
            self.ref_rx = re.compile(
                r"^(?:%s)/" % "|".join(re.escape(p) for p in sorted(roots)))
            # Publish availability only after all shape checks succeeded.
            self.spec = spec
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            self.spec = None

    @staticmethod
    def _strings(value):
        if value is None:
            return
        if not isinstance(value, (list, tuple)) or not all(isinstance(p, str) for p in value):
            raise TypeError("invalid_path_sequence")

    @staticmethod
    def _validate_steps(steps):
        pending = list(steps)
        count = 0
        while pending:
            item = pending.pop()
            count += 1
            if count > 10_000:
                raise ValueError("too_many_pipeline_steps")
            if not isinstance(item, Mapping):
                continue  # ordinary scalar wait/barrier steps are not test leaves
            if "label" in item and not isinstance(item["label"], str):
                raise TypeError("invalid_remote_label")
            if "group" in item and "steps" in item:
                if not isinstance(item["steps"], list):
                    raise TypeError("invalid_nested_steps")
                pending.extend(item["steps"])

    def _paths(self, command: str) -> tuple[str, ...]:
        refs = []
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        for token in lexer:
            # Handle complete literal path words and --option=path, never
            # a prefix truncated before a variable, quote concat or expansion.
            if token.startswith("--") and "=" in token:
                token = token.split("=", 1)[1]
            if not self.ref_rx.match(token):
                continue
            ref, separator, node = token.partition("::")
            if not re.fullmatch(r"[\w/.*-]+", ref):
                raise ValueError("unsupported_path_reference")
            if separator and not re.fullmatch(r"[\w.:\[\],=+-]+", node):
                raise ValueError("unsupported_pytest_node_suffix")
            refs.append(ref.rstrip("/"))
        refs = list(dict.fromkeys(refs))
        if len(refs) > 128:
            raise ValueError("too_many_path_references")
        paths = set()
        for ref in refs:
            parts = PurePosixPath(ref).parts
            if ".." in parts or "**" in ref:
                raise ValueError("unsafe_or_recursive_path_reference")
            matches = self.repo.glob(ref) if "*" in ref else (self.repo / ref,)
            found = False
            for path in matches:
                if len(paths) >= 512:
                    raise ValueError("too_many_glob_matches")
                resolved = path.resolve()
                if not resolved.is_relative_to(self.repo):
                    raise ValueError("path_reference_outside_repo")
                if path.exists():
                    found = True
                    paths.add(path.relative_to(self.repo).as_posix())
            if not found:
                raise ValueError("missing_path_reference")
        return tuple(sorted(paths))

    def _owner(self, paths: tuple[str, ...]) -> RemoteOwner:
        if not paths:
            return RemoteOwner(reason="no_repository_path_references")
        owners, unknown, shared = set(), False, False
        for path in paths:
            hits = [(len(pattern.rstrip("/")), module, pattern)
                    for module, patterns in self.patterns.items()
                    for pattern in patterns
                    if (path == pattern.rstrip("/") or
                        (pattern.endswith("/") and path.startswith(pattern)))]
            if not hits:
                unknown = True
                continue
            length = max(length for length, _, _ in hits)
            selected = [(module, pattern) for size, module, pattern in hits
                        if size == length]
            owners.update(module for module, _ in selected)
            shared |= any(pattern.rstrip("/") in self.universal
                          for _, pattern in selected)
        candidates = tuple(sorted(owners))
        if len(owners) > 1:
            return RemoteOwner(status="ambiguous",
                               reason="multiple_specific_declared_owners",
                               candidate_modules=candidates, paths=paths)
        if unknown or not owners:
            return RemoteOwner(reason="path_outside_specific_owner_footprints",
                               candidate_modules=candidates, paths=paths)
        return RemoteOwner(module=candidates[0],
                           status="declared_shared" if shared else "declared",
                           reason="explicit_declared_shared_root" if shared
                           else "specific_declared_path_footprint",
                           candidate_modules=candidates, paths=paths)

    def resolve(self, label: str, local_jobs: Mapping[str, Mapping]) -> RemoteOwner:
        if not isinstance(label, str):
            return RemoteOwner(reason="invalid_remote_label")
        slug = _label_to_slug(label)
        if not isinstance(local_jobs, Mapping):
            return RemoteOwner(reason="invalid_local_routing_view")
        local = local_jobs.get(slug) or {}
        if not isinstance(local, Mapping):
            return RemoteOwner(reason="invalid_local_route")
        if local.get("module") and not isinstance(local["module"], str):
            return RemoteOwner(reason="invalid_local_route")
        if local.get("module"):
            return RemoteOwner(module=str(local["module"]), status="local",
                               reason="preserved_existing_local_route")
        if self.spec is None:
            return RemoteOwner(reason="remote_routing_view_unavailable")
        candidates = []
        try:
            if len(self.spec.pipelines) > 32:
                raise ValueError("too_many_pipeline_files")
            for filename, source in self.spec.pipelines.items():
                path = self.repo / self.spec.yaml_dir / filename
                if not path.resolve().is_relative_to(self.repo):
                    raise ValueError("pipeline_outside_repo")
                if not path.is_file():
                    raise ValueError("missing_pipeline_file")
                if path.stat().st_size > 2_000_000:
                    raise ValueError("pipeline_file_too_large")
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(data, Mapping) or not isinstance(data.get("steps"), list):
                    raise ValueError("invalid_pipeline_shape")
                self._validate_steps(data["steps"])
                for job in _extract_steps(data["steps"], source, self.spec):
                    if job.slug == slug:
                        owner = self._owner(self._paths(job.command))
                        candidates.append(replace(owner, sources=(source,)))
            if not candidates:
                result = RemoteOwner(reason="no_declared_remote_label")
            else:
                owners = {c.module for c in candidates if c.module}
                statuses = {c.status for c in candidates}
                merged = dict(
                    sources=tuple(sorted({s for c in candidates for s in c.sources})),
                    paths=tuple(sorted({p for c in candidates for p in c.paths})),
                    candidate_modules=tuple(sorted({m for c in candidates
                                                    for m in c.candidate_modules})))
                if len(owners) == 1 and statuses <= {"declared", "declared_shared"}:
                    result = RemoteOwner(module=next(iter(owners)),
                                         status="declared_shared" if "declared_shared" in statuses
                                         else "declared",
                                         reason="explicit_declared_shared_root" if "declared_shared" in statuses
                                         else "specific_declared_path_footprint",
                                         **merged)
                elif "ambiguous" in statuses or len(owners) > 1:
                    result = RemoteOwner(status="ambiguous",
                                         reason="remote_label_owner_conflict",
                                         **merged)
                else:
                    result = RemoteOwner(reason="path_outside_specific_owner_footprints",
                                         **merged)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, yaml.YAMLError):
            result = RemoteOwner(reason="remote_routing_view_unavailable")
        return result
