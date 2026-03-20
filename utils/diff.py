"""
DocLens Diff — compare two extraction runs and surface what changed.
"""

import json
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum


class ChangeType(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


@dataclass
class SubmoduleDiff:
    name: str
    change_type: ChangeType
    old_description: str = ""
    new_description: str = ""
    similarity: float = 1.0


@dataclass
class ModuleDiff:
    module: str
    change_type: ChangeType
    old_description: str = ""
    new_description: str = ""
    description_similarity: float = 1.0
    submodule_diffs: list = field(default_factory=list)

    @property
    def added_submodules(self):
        return [s for s in self.submodule_diffs if s.change_type == ChangeType.ADDED]

    @property
    def removed_submodules(self):
        return [s for s in self.submodule_diffs if s.change_type == ChangeType.REMOVED]

    @property
    def modified_submodules(self):
        return [s for s in self.submodule_diffs if s.change_type == ChangeType.MODIFIED]


@dataclass
class ExtractionDiff:
    baseline_file: str
    current_file: str
    added_modules: list = field(default_factory=list)
    removed_modules: list = field(default_factory=list)
    modified_modules: list = field(default_factory=list)
    unchanged_modules: list = field(default_factory=list)

    @property
    def has_changes(self):
        return bool(self.added_modules or self.removed_modules or self.modified_modules)

    @property
    def summary(self):
        return {
            "modules_added": len(self.added_modules),
            "modules_removed": len(self.removed_modules),
            "modules_modified": len(self.modified_modules),
            "modules_unchanged": len(self.unchanged_modules),
            "submodules_added": sum(len(m.added_submodules) for m in self.modified_modules) +
                                 sum(len(m.submodule_diffs) for m in self.added_modules),
            "submodules_removed": sum(len(m.removed_submodules) for m in self.modified_modules) +
                                   sum(len(m.submodule_diffs) for m in self.removed_modules),
        }


def _text_similarity(a, b):
    if not a and not b: return 1.0
    if not a or not b: return 0.0
    if a == b: return 1.0
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0


MODIFICATION_THRESHOLD = 0.85


def _load_extraction(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array in {path}")
    return {item["module"]: item for item in data if "module" in item}


def diff_extractions(baseline_path, current_path):
    baseline = _load_extraction(baseline_path)
    current = _load_extraction(current_path)
    result = ExtractionDiff(baseline_file=baseline_path, current_file=current_path)

    baseline_names = set(baseline.keys())
    current_names = set(current.keys())

    for name in current_names - baseline_names:
        mod = current[name]
        result.added_modules.append(ModuleDiff(
            module=name, change_type=ChangeType.ADDED,
            new_description=mod.get("Description",""),
            submodule_diffs=[SubmoduleDiff(name=s, change_type=ChangeType.ADDED, new_description=d)
                             for s, d in mod.get("Submodules",{}).items()]
        ))

    for name in baseline_names - current_names:
        mod = baseline[name]
        result.removed_modules.append(ModuleDiff(
            module=name, change_type=ChangeType.REMOVED,
            old_description=mod.get("Description",""),
            submodule_diffs=[SubmoduleDiff(name=s, change_type=ChangeType.REMOVED, old_description=d)
                             for s, d in mod.get("Submodules",{}).items()]
        ))

    for name in baseline_names & current_names:
        old_mod = baseline[name]
        new_mod = current[name]
        old_desc = old_mod.get("Description","")
        new_desc = new_mod.get("Description","")
        desc_sim = _text_similarity(old_desc, new_desc)
        old_subs = old_mod.get("Submodules",{})
        new_subs = new_mod.get("Submodules",{})
        sub_diffs = []

        for sub_name in set(new_subs) - set(old_subs):
            sub_diffs.append(SubmoduleDiff(name=sub_name, change_type=ChangeType.ADDED, new_description=new_subs[sub_name]))
        for sub_name in set(old_subs) - set(new_subs):
            sub_diffs.append(SubmoduleDiff(name=sub_name, change_type=ChangeType.REMOVED, old_description=old_subs[sub_name]))
        for sub_name in set(old_subs) & set(new_subs):
            sim = _text_similarity(old_subs[sub_name], new_subs[sub_name])
            ct = ChangeType.MODIFIED if sim < MODIFICATION_THRESHOLD else ChangeType.UNCHANGED
            sub_diffs.append(SubmoduleDiff(name=sub_name, change_type=ct,
                old_description=old_subs[sub_name], new_description=new_subs[sub_name], similarity=sim))

        has_sub_changes = any(s.change_type != ChangeType.UNCHANGED for s in sub_diffs)
        module_changed = desc_sim < MODIFICATION_THRESHOLD or has_sub_changes
        module_diff = ModuleDiff(
            module=name,
            change_type=ChangeType.MODIFIED if module_changed else ChangeType.UNCHANGED,
            old_description=old_desc, new_description=new_desc,
            description_similarity=desc_sim, submodule_diffs=sub_diffs,
        )
        if module_changed:
            result.modified_modules.append(module_diff)
        else:
            result.unchanged_modules.append(module_diff)

    return result


def diff_to_dict(diff):
    def sub_to_dict(s):
        return {"name":s.name,"change_type":s.change_type,"old_description":s.old_description,
                "new_description":s.new_description,"similarity":round(s.similarity,3)}
    def mod_to_dict(m):
        return {"module":m.module,"change_type":m.change_type,"old_description":m.old_description,
                "new_description":m.new_description,"description_similarity":round(m.description_similarity,3),
                "submodule_diffs":[sub_to_dict(s) for s in m.submodule_diffs]}
    return {
        "baseline_file": diff.baseline_file,
        "current_file": diff.current_file,
        "summary": diff.summary,
        "added_modules": [mod_to_dict(m) for m in diff.added_modules],
        "removed_modules": [mod_to_dict(m) for m in diff.removed_modules],
        "modified_modules": [mod_to_dict(m) for m in diff.modified_modules],
    }
