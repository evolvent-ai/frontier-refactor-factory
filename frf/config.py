"""YAML-based configuration for multi-job batch runs.

Two levels:

  JobConfig   one scale/form pair with its own budget and language settings
  RunConfig   the envelope: all jobs plus infrastructure settings

from_yaml / from_dict are the constructors a caller uses. to_yaml lets a run write what it used,
so the run is reproducible from what is written into provenance.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class JobConfig:
    scale: str
    form: str                   # "inplace" or "cross"
    source_language: str = ""   # "" = any
    target_language: str = ""   # "" = same as source for inplace
    budget: int = 10
    index: str = ""             # which source index to use; "" = auto
    subset: str = ""            # index-specific filter

    def __post_init__(self) -> None:
        if self.scale not in ("kernel", "module", "package", "repo"):
            raise ValueError("unknown scale %r" % self.scale)
        if self.form not in ("inplace", "cross"):
            raise ValueError("form must be 'inplace' or 'cross', got %r" % self.form)
        if self.budget < 1:
            raise ValueError("budget must be at least 1, got %d" % self.budget)


@dataclass
class RunConfig:
    jobs: list[JobConfig]
    output_dir: str = "tasks"
    freeze_runs: int = 5
    max_concurrent: int = 32
    llm_max_concurrent: int = 10
    llm_calls_per_minute: int = 60
    checkpoint_file: str = ""       # "" = auto-generate from timestamp
    sandboxed: bool = True

    @classmethod
    def from_yaml(cls, path: str) -> "RunConfig":
        """Load from a YAML file."""
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required for YAML config support: pip install pyyaml") from exc
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "RunConfig":
        """Construct from a plain dict (e.g. parsed from YAML or JSON)."""
        raw_jobs = data.get("jobs", [])
        jobs = []
        for j in raw_jobs:
            j = dict(j)
            jobs.append(JobConfig(
                scale=j["scale"],
                form=j.get("form", "inplace"),
                source_language=j.get("source_language", ""),
                target_language=j.get("target_language", ""),
                budget=int(j.get("budget", 10)),
                index=j.get("index", ""),
                subset=j.get("subset", ""),
            ))

        return cls(
            jobs=jobs,
            output_dir=str(data.get("output_dir", "tasks")).rstrip("/"),
            freeze_runs=int(data.get("freeze_runs", 5)),
            max_concurrent=int(data.get("max_concurrent", 32)),
            llm_max_concurrent=int(data.get("llm_max_concurrent", 10)),
            llm_calls_per_minute=int(data.get("llm_calls_per_minute", 60)),
            checkpoint_file=str(data.get("checkpoint_file", "")),
            sandboxed=bool(data.get("sandboxed", True)),
        )

    def to_yaml(self) -> str:
        """Serialise to a YAML string (for writing provenance)."""
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required for YAML config support: pip install pyyaml") from exc

        data: dict[str, Any] = {
            "output_dir": self.output_dir,
            "freeze_runs": self.freeze_runs,
            "max_concurrent": self.max_concurrent,
            "llm_max_concurrent": self.llm_max_concurrent,
            "llm_calls_per_minute": self.llm_calls_per_minute,
            "checkpoint_file": self.checkpoint_file,
            "sandboxed": self.sandboxed,
            "jobs": [],
        }
        for job in self.jobs:
            entry: dict[str, Any] = {"scale": job.scale, "form": job.form,
                                     "budget": job.budget}
            if job.source_language:
                entry["source_language"] = job.source_language
            if job.target_language:
                entry["target_language"] = job.target_language
            if job.index:
                entry["index"] = job.index
            if job.subset:
                entry["subset"] = job.subset
            data["jobs"].append(entry)

        return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def to_json(self) -> dict:
        """Serialise configuration without credentials."""
        return {
            "output_dir": self.output_dir,
            "freeze_runs": self.freeze_runs,
            "max_concurrent": self.max_concurrent,
            "llm_max_concurrent": self.llm_max_concurrent,
            "llm_calls_per_minute": self.llm_calls_per_minute,
            "checkpoint_file": self.checkpoint_file,
            "sandboxed": self.sandboxed,
            "jobs": [
                {k: v for k, v in {
                    "scale": j.scale, "form": j.form,
                    "source_language": j.source_language,
                    "target_language": j.target_language,
                    "budget": j.budget,
                    "index": j.index,
                    "subset": j.subset,
                }.items() if v or k in ("scale", "form", "budget")}
                for j in self.jobs
            ],
        }


__all__ = ["JobConfig", "RunConfig"]
