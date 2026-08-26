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
    # In configured roll mode, budget is the number of fully emitted tasks. This bounds how much
    # unsuitable material may be consumed while trying to reach that target.
    max_attempts: int = 0          # 0 = max(budget, budget * 10)
    index: str = ""             # which source index to use; "" = auto
    subset: str = ""            # index-specific filter

    def __post_init__(self) -> None:
        if self.scale not in ("kernel", "module", "package", "repo"):
            raise ValueError("unknown scale %r" % self.scale)
        if self.form not in ("inplace", "cross"):
            raise ValueError("form must be 'inplace' or 'cross', got %r" % self.form)
        if self.budget < 1:
            raise ValueError("budget must be at least 1, got %d" % self.budget)
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be non-negative")
        if self.max_attempts and self.max_attempts < self.budget:
            raise ValueError("max_attempts cannot be smaller than the emitted-task budget")


@dataclass
class RunConfig:
    jobs: list[JobConfig]
    output_dir: str = "tasks"
    freeze_runs: int = 5
    max_concurrent: int = 32
    e2b_max_active: int = 8
    llm_max_concurrent: int = 10
    llm_calls_per_minute: int = 60
    # Harbor agent review is an optional release-quality gate. It is deliberately off for
    # throughput runs; when enabled, repairable rubric findings may be retried once by the runner.
    harbor_check: bool = False
    harbor_repair: bool = True
    harbor_max_repairs: int = 1
    checkpoint_file: str = ""       # "" = auto-generate from timestamp
    ledger_file: str = ""
    sandboxed: bool = True

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if self.e2b_max_active < 1:
            raise ValueError("e2b_max_active must be at least 1")
        if self.freeze_runs < 2:
            raise ValueError("freeze_runs must be at least 2")
        if self.harbor_max_repairs < 0:
            raise ValueError("harbor_max_repairs must be non-negative")

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
                max_attempts=int(j.get("max_attempts", 0)),
                index=j.get("index", ""),
                subset=j.get("subset", ""),
            ))

        return cls(
            jobs=jobs,
            output_dir=str(data.get("output_dir", "tasks")).rstrip("/"),
            freeze_runs=int(data.get("freeze_runs", 5)),
            max_concurrent=int(data.get("max_concurrent", 32)),
            e2b_max_active=int(data.get("e2b_max_active", 8)),
            llm_max_concurrent=int(data.get("llm_max_concurrent", 10)),
            llm_calls_per_minute=int(data.get("llm_calls_per_minute", 60)),
            harbor_check=bool(data.get("harbor_check", False)),
            harbor_repair=bool(data.get("harbor_repair", True)),
            harbor_max_repairs=int(data.get("harbor_max_repairs", 1)),
            checkpoint_file=str(data.get("checkpoint_file", "")),
            ledger_file=str(data.get("ledger_file", "")),
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
            "e2b_max_active": self.e2b_max_active,
            "llm_max_concurrent": self.llm_max_concurrent,
            "llm_calls_per_minute": self.llm_calls_per_minute,
            "harbor_check": self.harbor_check,
            "harbor_repair": self.harbor_repair,
            "harbor_max_repairs": self.harbor_max_repairs,
            "checkpoint_file": self.checkpoint_file,
            "ledger_file": self.ledger_file,
            "sandboxed": self.sandboxed,
            "jobs": [],
        }
        for job in self.jobs:
            entry: dict[str, Any] = {"scale": job.scale, "form": job.form,
                                     "budget": job.budget}
            if job.max_attempts:
                entry["max_attempts"] = job.max_attempts
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
            "e2b_max_active": self.e2b_max_active,
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
                    "max_attempts": j.max_attempts,
                    "index": j.index,
                    "subset": j.subset,
                }.items() if v or k in ("scale", "form", "budget")}
                for j in self.jobs
            ],
        }


__all__ = ["JobConfig", "RunConfig"]
