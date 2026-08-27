"""Unit tests for the idle-stall recovery helpers (Issue #94).

The helpers read the REAL `/proc` in production (ppid chain from
`/proc/<pid>/stat` + process start time — never a name guess). The unit
tests build a fake procfs in `tmp_path` and point `pi_recovery.PROC` at
it, so no real process is ever signaled here.
"""
import os
import signal
import sys

import pytest

import pi_recovery


# Fake clock constants: btime=1_000_000 s, CLK_TCK=100 -> one tick = 0.01 s.
FAKE_BTIME = 1_000_000.0
FAKE_HZ = 100.0


def make_procfs(tmp_path, processes, btime=FAKE_BTIME):
    """Build a fake procfs: `processes` is a list of
    `(pid, comm, ppid, starttime_ticks, cmdline_bytes_or_None)`."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(f"btime {int(btime)}\n", encoding="utf-8")
    for pid, comm, ppid, starttime, cmdline in processes:
        entry = proc / str(pid)
        entry.mkdir()
        # stat layout: pid (comm) state ppid ... starttime (field 22).
        # After the LAST ')' the fields start at index 0 (state); ppid is
        # index 1 and starttime index 19.
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(starttime)]
        (entry / "stat").write_text(
            f"{pid} ({comm}) " + " ".join(fields), encoding="utf-8",
        )
        if cmdline is not None:
            (entry / "cmdline").write_bytes(cmdline)
    return proc


def start_epoch(starttime_ticks):
    return FAKE_BTIME + starttime_ticks / FAKE_HZ


# --- /proc parsing -----------------------------------------------------------


def test_process_ppid_reads_field_4(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [(42, "bash", 7, 100, b"bash")])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_ppid(42) == 7


def test_process_ppid_returns_none_for_gone_process(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_ppid(99) is None


def test_process_ppid_returns_none_for_malformed_stat(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [])
    (proc / "5").mkdir()
    (proc / "5" / "stat").write_text("garbage without parens",
                                     encoding="utf-8")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_ppid(5) is None


def test_process_ppid_returns_none_for_non_numeric_ppid(tmp_path,
                                                        monkeypatch):
    proc = make_procfs(tmp_path, [])
    (proc / "6").mkdir()
    (proc / "6" / "stat").write_text(
        "6 (bad) S x " + " ".join(["0"] * 18), encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_ppid(6) is None


def test_process_start_epoch_converts_ticks_to_epoch(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [(42, "bash", 7, 1234, b"bash")])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_start_epoch(
        42, btime=FAKE_BTIME, hz=FAKE_HZ,
    ) == start_epoch(1234)


def test_process_start_epoch_returns_none_for_gone_process(tmp_path,
                                                           monkeypatch):
    proc = make_procfs(tmp_path, [])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_start_epoch(
        99, btime=FAKE_BTIME, hz=FAKE_HZ,
    ) is None


def test_process_start_epoch_returns_none_for_short_stat(tmp_path,
                                                         monkeypatch):
    proc = make_procfs(tmp_path, [])
    (proc / "7").mkdir()
    (proc / "7" / "stat").write_text("7 (short) S 1", encoding="utf-8")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_start_epoch(
        7, btime=FAKE_BTIME, hz=FAKE_HZ,
    ) is None


def test_process_start_epoch_returns_none_for_bad_starttime(tmp_path,
                                                            monkeypatch):
    proc = make_procfs(tmp_path, [])
    (proc / "8").mkdir()
    (proc / "8" / "stat").write_text(
        "8 (bad) S 1 " + " ".join(["0"] * 17) + " x", encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_start_epoch(
        8, btime=FAKE_BTIME, hz=FAKE_HZ,
    ) is None


def test_boot_time_reads_btime_from_proc_stat(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [], btime=1234567)
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.boot_time() == 1234567.0


def test_boot_time_fails_fast_without_btime(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("intr 1\n", encoding="utf-8")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    with pytest.raises(RuntimeError, match="btime"):
        pi_recovery.boot_time()


def test_cmdline_of_joins_null_separated_arguments(tmp_path, monkeypatch):
    proc = make_procfs(
        tmp_path, [(42, "bash", 7, 100, b"/usr/bin/python3\x00-m\x00pytest")],
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.cmdline_of(42) == "/usr/bin/python3 -m pytest"


def test_cmdline_of_falls_back_to_comm_when_cmdline_empty(tmp_path,
                                                          monkeypatch):
    proc = make_procfs(tmp_path, [(42, "kthreadd", 7, 100, b"")])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.cmdline_of(42) == "kthreadd"


def test_cmdline_of_empty_when_process_gone(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.cmdline_of(99) == ""


def test_cmdline_of_empty_when_stat_malformed(tmp_path, monkeypatch):
    # Empty cmdline and a stat line without a parenthesized comm: the
    # comm fallback yields empty, never a crash.
    proc = make_procfs(tmp_path, [])
    (proc / "9").mkdir()
    (proc / "9" / "stat").write_text("garbage", encoding="utf-8")
    (proc / "9" / "cmdline").write_bytes(b"")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.cmdline_of(9) == ""


def test_comm_empty_when_parenthesis_unbalanced(tmp_path, monkeypatch):
    # A `(` with no closing `)` (end <= start): the comm fallback
    # yields empty, never a crash.
    proc = make_procfs(tmp_path, [])
    (proc / "10").mkdir()
    (proc / "10" / "stat").write_text("10 (unbalanced S 1", encoding="utf-8")
    (proc / "10" / "cmdline").write_bytes(b"")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.cmdline_of(10) == ""


def test_cmdline_of_empty_when_stat_gone_after_empty_cmdline(
    tmp_path, monkeypatch,
):
    # The process exits between the empty cmdline read and the stat
    # fallback read: empty, never a crash.
    proc = make_procfs(tmp_path, [])
    (proc / "11").mkdir()
    (proc / "11" / "cmdline").write_bytes(b"")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.cmdline_of(11) == ""


def test_cmdline_of_empty_when_cmdline_file_missing(tmp_path, monkeypatch):
    # No cmdline file at all (the process exited before the read): the
    # result is empty — the comm fallback only applies to an EMPTY
    # cmdline (kernel threads), not to a gone process.
    proc = make_procfs(tmp_path, [(42, "kworker", 2, 100, None)])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.cmdline_of(42) == ""


# --- descendant discovery ----------------------------------------------------


def test_descendant_pids_follows_ppid_chain_to_all_depths(
    tmp_path, monkeypatch,
):
    # 100 (pi) -> 200 (bash) -> 300 (pytest); 400 is a sibling of 200's
    # child only via the chain; 500 is unrelated.
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 20, b"bash"),
        (300, "pytest", 200, 30, b"pytest"),
        (400, "python", 300, 40, b"python"),
        (500, "unrelated", 1, 50, b"unrelated"),
    ])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.descendant_pids(100) == [200, 300, 400]


def test_descendant_pids_empty_for_leaf_process(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [(100, "pi", 1, 10, b"pi")])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.descendant_pids(100) == []


def test_descendant_pids_empty_when_procfs_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_recovery, "PROC", tmp_path / "no-such-proc")
    assert pi_recovery.descendant_pids(100) == []


def test_descendant_pids_skips_gone_and_malformed_entries(tmp_path,
                                                          monkeypatch):
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 20, b"bash"),
    ])
    (proc / "999").mkdir()  # a directory without a stat file
    (proc / "notes").mkdir()  # a non-numeric entry
    (proc / "300").mkdir()
    (proc / "300" / "stat").write_text("garbage", encoding="utf-8")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.descendant_pids(100) == [200]


def test_descendant_pids_skips_non_numeric_ppid(tmp_path, monkeypatch):
    # A corrupted stat line (non-numeric ppid) is skipped, never a
    # crash — the rest of the tree is still walked.
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 20, b"bash"),
    ])
    (proc / "300").mkdir()
    (proc / "300" / "stat").write_text(
        "300 (bad) S x " + " ".join(["0"] * 18), encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.descendant_pids(100) == [200]


def test_descendant_pids_survives_ppid_cycle(tmp_path, monkeypatch):
    # A corrupted procfs where a process is its own parent (ppid cycle)
    # must not loop forever: the seen-set breaks the cycle.
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 20, b"bash"),
    ])
    (proc / "300").mkdir()
    (proc / "300" / "stat").write_text(
        "300 (cycle) S 300 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    # The contract: no hang — the cycle is broken by the seen-set (the
    # process is not its own descendant).
    assert pi_recovery.descendant_pids(300) == []


# --- idle target selection ---------------------------------------------------


def test_find_idle_descendants_selects_pre_idle_start_descendants_only(
    tmp_path, monkeypatch,
):
    # 200 started long before the idle window (the hung tool), 300
    # started AFTER it (a newer tool call): only 200 is a target.
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 100, b"/bin/bash\x00-c\x00pytest"),
        (300, "pytest", 200, 5000, b"pytest"),
    ])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    idle_start = start_epoch(1000)  # between the two starts
    targets = pi_recovery.find_idle_descendants(
        100, idle_start, btime=FAKE_BTIME, hz=FAKE_HZ,
    )
    assert targets == [
        {"pid": 200, "cmdline": "/bin/bash -c pytest"},
    ]


def test_find_idle_descendants_includes_process_started_at_idle_start(
    tmp_path, monkeypatch,
):
    # A process that started exactly at the idle start belongs to the
    # stalled scene (boundary is inclusive).
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 1000, b"bash"),
    ])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    targets = pi_recovery.find_idle_descendants(
        100, start_epoch(1000), btime=FAKE_BTIME, hz=FAKE_HZ,
    )
    assert [t["pid"] for t in targets] == [200]


def test_find_idle_descendants_skips_targets_that_died_before_read(
    tmp_path, monkeypatch,
):
    # The descendant exists in the tree walk but its stat is unreadable
    # (it exited between the walk and the start-time read): skipped.
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 100, b"bash"),
    ])
    (proc / "200" / "stat").unlink()
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.find_idle_descendants(
        100, start_epoch(1000), btime=FAKE_BTIME, hz=FAKE_HZ,
    ) == []


def test_find_idle_descendants_skips_unreadable_start_time(
    tmp_path, monkeypatch,
):
    # A descendant whose stat vanishes between the tree walk and the
    # start-time read (it exited in the gap) is skipped, never a
    # crash; the remaining targets are still reported.
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 100, b"bash"),
        (300, "pytest", 100, 200, b"pytest"),
    ])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    real_read = pi_recovery._read_stat

    def flaky_read(pid):
        raw = real_read(pid)
        if pid == 300 and raw is not None:
            # First read (the tree walk) sees the process; the second
            # read (the start time) finds it gone.
            (proc / "300" / "stat").unlink()
        return raw

    monkeypatch.setattr(pi_recovery, "_read_stat", flaky_read)
    targets = pi_recovery.find_idle_descendants(
        100, start_epoch(1000), btime=FAKE_BTIME, hz=FAKE_HZ,
    )
    assert [t["pid"] for t in targets] == [200]


def test_find_idle_descendants_uses_real_clock_when_not_given(
    tmp_path, monkeypatch,
):
    # Without explicit btime/hz the real boot time and CLK_TCK are used:
    # a process that started just now is NOT pre-idle-start for an
    # idle start in the past.
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "bash", 100, 100, b"bash"),
    ])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    real_btime = pi_recovery.boot_time()
    real_hz = pi_recovery.clk_tck()
    # starttime=100 ticks after the real boot: a very old process.
    targets = pi_recovery.find_idle_descendants(
        100, real_btime + 10_000,
    )
    assert [t["pid"] for t in targets] == [200]
    # Sanity: the real clock constants are sane (guards the test itself).
    assert real_btime > 0 and real_hz > 0


# --- signaling ---------------------------------------------------------------


def test_pid_alive_true_for_running_process():
    pid = os.getpid()
    assert pi_recovery.pid_alive(pid) is True


def test_pid_alive_true_for_process_owned_by_other_user(monkeypatch):
    # EPERM means the process EXISTS (owned by another user): alive —
    # the runner must not signal it, only report it.
    def deny(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(pi_recovery.os, "kill", deny)
    assert pi_recovery.pid_alive(42) is True


def test_pid_alive_false_for_gone_process(monkeypatch):
    def raise_esrch(pid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(pi_recovery.os, "kill", raise_esrch)
    assert pi_recovery.pid_alive(42) is False


def test_pid_alive_false_for_real_exited_process():
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert pi_recovery.pid_alive(child.pid) is False


def test_signal_pid_reports_sent_for_running_process():
    pid = os.getpid()
    # Signal 0: no signal is delivered, only the reachability check.
    assert pi_recovery.signal_pid(pid, 0) == "sent"


def test_signal_pid_reports_already_dead_for_gone_process(monkeypatch):
    def raise_esrch(pid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(pi_recovery.os, "kill", raise_esrch)
    assert pi_recovery.signal_pid(42, signal.SIGTERM) == "already_dead"


def test_signal_pid_reports_failed_for_unreachable_process(
    monkeypatch,
):
    def deny(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(pi_recovery.os, "kill", deny)
    assert pi_recovery.signal_pid(42, signal.SIGTERM) == \
        "failed: [Errno 1] Operation not permitted"
