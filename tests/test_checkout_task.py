import os
import json
import subprocess

from frf.core.contract import CheckoutContract, Provenance
from frf.core.harbor import Package
from frf.observe.checkout_task import drive, write
from frf.automation import emit_checkout_task


def test_checkout_task_preserves_real_local_imports_and_replays(tmp_path):
    root = tmp_path / "source"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "helper.py").write_text("VALUE = 7\n")
    (root / "pkg" / "main.py").write_text("from .helper import VALUE\nassert VALUE == 7\n")
    contract = CheckoutContract(
        str(root), ("pkg/main.py",),
        verify=(("python3", "-m", "pkg.main"),),
        provenance=Provenance("git:example/project@abc", "test-derived",
                              evidence=("pkg/main.py",)))
    destination = str(tmp_path / "task")
    write(destination, Package("native-checkout", "module", "d", "i", "python"), contract)
    assert os.path.exists(os.path.join(destination, "environment", "pkg", "helper.py"))
    assert drive(destination) == (1, 1)


def test_checkout_production_entry_requires_self_replay(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "check.py").write_text("assert True\n")
    contract = CheckoutContract(str(root), ("check.py",),
        verify=(("python3", "check.py"),),
        provenance=Provenance("git:example/project@abc", "test-derived", evidence=("check.py",)))
    package = Package("native", "package", "d", "i", "python")
    assert emit_checkout_task(destination=str(tmp_path / "task"), package=package,
                              contract=contract) == (1, 1)


def test_checkout_workload_compares_hidden_reference_and_enforces_speed(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "value.txt").write_text("7\n")
    # This is part of the sourced checkout, but the emitted verifier invokes its hidden copy and
    # passes the candidate root explicitly. Editing the candidate's work.py cannot alter judging.
    (root / "work.py").write_text(
        "import json, pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "value = int((root / 'value.txt').read_text())\n"
        "time.sleep(float((root / 'delay.txt').read_text() if (root / 'delay.txt').exists() else 0))\n"
        "print(json.dumps({'value': value}))\n")
    contract = CheckoutContract(
        str(root), ("value.txt",),
        verify=(("python3", "-c", "assert True"),),
        benchmark=(("python3", "work.py", "{workspace}"),),
        timing_runs=3,
        provenance=Provenance("git:example/project@abc", "native-workload",
                              evidence=("work.py",)))
    destination = tmp_path / "task"
    write(str(destination), Package("native", "module", "d", "i", "python"), contract)
    assert drive(str(destination)) == (1, 1)

    # A slower candidate remains functionally correct; the measured speedup is reported honestly.
    (destination / "environment" / "delay.txt").write_text("0.01")
    reward = tmp_path / "reward.json"
    done = subprocess.run(["python3", str(destination / "tests" / "verify.py"),
                           "--task-root", str(destination / "tests"),
                           "--workspace", str(destination / "environment")],
                          env={**os.environ, "REWARD_PATH": str(reward)}, check=False)
    assert done.returncode == 0
    report = json.loads(reward.read_text())
    assert report["correct"] is True
    assert report["speedup"] < 1.2

    # A changed candidate output is rejected independently of timing.
    (destination / "environment" / "value.txt").write_text("8\n")
    subprocess.run(["python3", str(destination / "tests" / "verify.py"),
                    "--task-root", str(destination / "tests"),
                    "--workspace", str(destination / "environment")],
                   env={**os.environ, "REWARD_PATH": str(reward)}, check=False)
    report = json.loads(reward.read_text())
    assert report["correct"] is False
    assert "differs" in report["note"]
