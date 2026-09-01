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
    `(pid, comm, ppid, starttime_ticks, cmdline_bytes_or_None, state)`
    (the state char is optional, default `S`)."""
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(f"btime {int(btime)}\n", encoding="utf-8")
    for item in processes:
        pid, comm, ppid, starttime, cmdline = item[:5]
        state = item[5] if len(item) > 5 else "S"
        entry = proc / str(pid)
        entry.mkdir()
        # stat layout: pid (comm) state ppid ... starttime (field 22).
        # After the LAST ')' the fields start at index 0 (state); ppid is
        # index 1 and starttime index 19.
        fields = [state, str(ppid)] + ["0"] * 17 + [str(starttime)]
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


def test_find_idle_descendants_skips_zombies(tmp_path, monkeypatch):
    # Issue #169: a descendant that already exited but was not reaped
    # yet (state Z, the parent is busy) is DEAD — signaling it is
    # meaningless and its empty cmdline would fall back to the comm
    # name (a lie like `timeout` for an exited `timeout 0.6 sleep 300`).
    # Only running states are targets.
    proc = make_procfs(tmp_path, [
        (100, "pi", 1, 10, b"pi"),
        (200, "timeout", 100, 100, b"timeout\x000.6\x00sleep\x00300",
         "Z"),
        (300, "sleep", 100, 200, b"sleep\x00300"),
    ])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    targets = pi_recovery.find_idle_descendants(
        100, start_epoch(1000), btime=FAKE_BTIME, hz=FAKE_HZ,
    )
    assert [t["pid"] for t in targets] == [300]


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


# --- Issue #169: evidence-based stall detection ------------------------------


def test_process_state_reads_stat_state_char(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [(42, "bash", 7, 100, b"bash")])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_state(42) == "S"


def test_process_state_none_for_gone_process(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_state(99) is None


def test_process_state_none_for_malformed_stat(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [])
    (proc / "5").mkdir()
    (proc / "5" / "stat").write_text("garbage without parens",
                                     encoding="utf-8")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_state(5) is None


def test_process_start_monotonic_reads_field_22(tmp_path, monkeypatch):
    # stat field 22 (starttime) in ticks since boot, converted with the
    # given hz: 100 ticks at hz=100 -> 1.0 s since boot. The value is a
    # MONOTONIC offset (Issue #169): it is compared against
    # `time.monotonic()`, never against the realtime epoch — a realtime
    # step after boot (NTP) must not skew a process's age.
    proc = make_procfs(tmp_path, [(42, "bash", 7, 100, b"bash")])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_start_monotonic(42, hz=FAKE_HZ) == 1.0


def test_process_start_monotonic_none_for_gone_process(tmp_path, monkeypatch):
    proc = make_procfs(tmp_path, [])
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_start_monotonic(99, hz=FAKE_HZ) is None


def test_process_start_monotonic_none_for_malformed_stat(
    tmp_path, monkeypatch,
):
    proc = make_procfs(tmp_path, [])
    (proc / "5").mkdir()
    (proc / "5" / "stat").write_text("garbage without parens",
                                     encoding="utf-8")
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_start_monotonic(5, hz=FAKE_HZ) is None


def test_process_start_monotonic_none_for_non_numeric_starttime(
    tmp_path, monkeypatch,
):
    proc = make_procfs(tmp_path, [])
    (proc / "6").mkdir()
    # 17 zero fields after ppid, then a non-numeric starttime at index 19.
    (proc / "6" / "stat").write_text(
        "6 (bad) S 1 " + " ".join(["0"] * 17 + ["x"]), encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.process_start_monotonic(6, hz=FAKE_HZ) is None


def test_timeout_duration_plain_seconds(tmp_path):
    # The prompt contract form: `timeout <seconds> ...` — the duration
    # is the immediate next token after the wrapper word.
    assert pi_recovery.timeout_duration("timeout 240 pytest tests/") == 240.0


def test_timeout_duration_after_bash_c_prefix(tmp_path):
    # The Pi bash tool spawns `bash -c <command>`: the wrapper word is
    # found wherever it stands in the command line.
    assert pi_recovery.timeout_duration(
        "/bin/bash -c timeout 240 pytest tests/",
    ) == 240.0


def test_timeout_duration_suffixes(tmp_path):
    # coreutils duration suffixes: s, m, h, d.
    assert pi_recovery.timeout_duration("timeout 4m pytest") == 240.0
    assert pi_recovery.timeout_duration("timeout 1.5m pytest") == 90.0
    assert pi_recovery.timeout_duration("timeout 1h pytest") == 3600.0
    assert pi_recovery.timeout_duration("timeout 2d pytest") == 172800.0


def test_timeout_duration_none_without_clear_timeout(tmp_path):
    # No wrapper word, a word that merely contains `timeout`, or a
    # missing/unparseable duration: NOT a clear timeout (fail-safe, the
    # existing recovery behavior applies).
    assert pi_recovery.timeout_duration("pytest tests/") is None
    assert pi_recovery.timeout_duration("timeoutctl 240 x") is None
    assert pi_recovery.timeout_duration("timeout pytest") is None
    assert pi_recovery.timeout_duration("timeout abc pytest") is None
    assert pi_recovery.timeout_duration("timeout -k 5 240 pytest") is None
    assert pi_recovery.timeout_duration("timeout 0 pytest") is None


def test_timeout_duration_edge_tokens(tmp_path):
    # A `timeout` as the LAST token has no duration at all; an empty
    # duration token, a multi-dot number and a bare `.` are not
    # parseable durations.
    assert pi_recovery.timeout_duration("bash -c timeout") is None
    assert pi_recovery.timeout_duration("timeout '' x") is None
    assert pi_recovery.timeout_duration("timeout 1.2.3 x") is None
    assert pi_recovery.timeout_duration("timeout . x") is None


def test_upstream_alive_true_for_established_tcp_socket(
    tmp_path, monkeypatch,
):
    # The process holds a socket whose inode appears in /proc/net/tcp
    # with a non-zero remote address: the upstream connection is live.
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "stat").write_text(
        "42 (pi) S 1 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    (proc / "42" / "fd" / "7").symlink_to("socket:[12345]")
    (proc / "42" / "fd" / "8").symlink_to("anon_inode:[eventpoll]")
    (proc / "net").mkdir()
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st inode\n"
        "   0: 0100007F:1F90 0100007F:8762 01 00000000:00000000 00:00000000 00000000     0        0 12345 1 00000000d7514f4a 100\n",
        encoding="utf-8",
    )
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address remote_address st inode\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is True


def test_upstream_alive_false_for_listen_only_socket(
    tmp_path, monkeypatch,
):
    # A socket with a zero remote address (LISTEN) is not an upstream
    # connection: no remote peer, no live model request.
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "stat").write_text(
        "42 (pi) S 1 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    (proc / "42" / "fd" / "7").symlink_to("socket:[12345]")
    (proc / "net").mkdir()
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st inode\n"
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345 1 00000000d7514f4a 100\n",
        encoding="utf-8",
    )
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address remote_address st inode\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is False


def test_upstream_alive_true_for_established_tcp6_socket(
    tmp_path, monkeypatch,
):
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "stat").write_text(
        "42 (pi) S 1 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    (proc / "42" / "fd" / "7").symlink_to("socket:[999]")
    (proc / "net").mkdir()
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st inode\n",
        encoding="utf-8",
    )
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address remote_address st inode\n"
        "   0: 00000000000000000000000000000001:01BB "
        "00000000000000000000000000000002:01BB 01 00000000:00000000 00:00000000 00000000     0        0 999 1 00000000d7514f4a 100\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is True


def test_upstream_alive_false_for_close_wait_socket(
    tmp_path, monkeypatch,
):
    # The peer (the upstream) closed the connection: the socket sits in
    # CLOSE_WAIT (08) with the remote address still printed — the
    # request is dead, the runner must not see a live upstream.
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "stat").write_text(
        "42 (pi) S 1 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    (proc / "42" / "fd" / "7").symlink_to("socket:[12345]")
    (proc / "net").mkdir()
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st inode\n"
        "   0: 0100007F:1F90 0100007F:8762 08 00000000:00000000 "
        "00:00000000 00000000     0        0 12345 1 00000000d7514f4a 100\n",
        encoding="utf-8",
    )
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address remote_address st inode\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is False


def test_upstream_alive_false_for_gone_process(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is False


def test_upstream_alive_false_when_socket_has_no_remote_entry(
    tmp_path, monkeypatch,
):
    # The process holds a socket (e.g. AF_UNIX: `socket:[ino]` in the fd
    # table but no /proc/net/tcp entry with a remote address): no live
    # upstream TCP connection.
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "stat").write_text(
        "42 (pi) S 1 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    (proc / "42" / "fd" / "7").symlink_to("socket:[777]")
    (proc / "net").mkdir()
    # The only TCP entry belongs to ANOTHER process's socket.
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st inode\n"
        "   0: 0100007F:1F90 0100007F:8762 01 00000000:00000000 00:00000000 00000000     0        0 555 1 00000000d7514f4a 100\n",
        encoding="utf-8",
    )
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address remote_address st inode\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is False


def test_upstream_alive_skips_fd_entry_that_cannot_be_read(
    tmp_path, monkeypatch,
):
    # An fd entry that vanishes between the directory listing and the
    # readlink (OSError) is skipped, never a crash — the remaining
    # socket is still evaluated.
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "stat").write_text(
        "42 (pi) S 1 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    # A REGULAR file in the fd table: readlink on it raises OSError.
    (proc / "42" / "fd" / "8").write_text("nope", encoding="utf-8")
    (proc / "42" / "fd" / "7").symlink_to("socket:[12345]")
    (proc / "net").mkdir()
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st inode\n"
        "   0: 0100007F:1F90 0100007F:8762 01 00000000:00000000 00:00000000 00000000     0        0 12345 1 00000000d7514f4a 100\n",
        encoding="utf-8",
    )
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address remote_address st inode\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is True


def test_upstream_alive_skips_unreadable_net_tables(
    tmp_path, monkeypatch,
):
    # A /proc/net table that cannot be read (OSError) is skipped, never
    # a crash: with no readable table there is no live evidence.
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "stat").write_text(
        "42 (pi) S 1 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    (proc / "42" / "fd" / "7").symlink_to("socket:[12345]")
    (proc / "net").mkdir()
    # `tcp` is a DIRECTORY: read_text raises IsADirectoryError (OSError).
    (proc / "net" / "tcp").mkdir()
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address remote_address st inode\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is False


# --- slots_idle (Issue #233: request-level liveness probe) ---------------
# The probe is the fast path for the #231 swallow scene: the model process
# is alive and the connection ESTABLISHED, but the slot is idle (the request
# was accepted and never scheduled). It reads the model's /slots endpoint
# (verified against the real llama-server: a JSON LIST of slot objects, each
# with an `is_processing` bool). It is a pure bypass: any probe failure is
# inconclusive (None), never an error.


class _FakeSlotsResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, *, result=None, exc=None):
    calls = {}

    def fake_urlopen(url, timeout=None):
        calls["url"] = url
        calls["timeout"] = timeout
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(pi_recovery.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_slots_idle_true_when_every_slot_is_idle(monkeypatch):
    # The #231 scene: the endpoint is reachable and every slot reports
    # is_processing=false — the model is NOT generating, the request was
    # swallowed. This is the swallow evidence (True).
    body = (
        b'[{"id": 0, "is_processing": false, "id_task": 561412, '
        b'"n_decoded_tokens": null}]'
    )
    _patch_urlopen(
        monkeypatch, result=_FakeSlotsResponse(200, body),
    )
    assert pi_recovery.slots_idle("http://127.0.0.1:18082/slots") is True


def test_slots_idle_true_with_multiple_idle_slots(monkeypatch):
    # --parallel N: every slot idle is still the swallow evidence.
    body = b'[{"id": 0, "is_processing": false}, {"id": 1, "is_processing": false}]'
    _patch_urlopen(monkeypatch, result=_FakeSlotsResponse(200, body))
    assert pi_recovery.slots_idle("http://x/slots") is True


def test_slots_idle_false_when_a_slot_is_processing(monkeypatch):
    # A slot is generating (is_processing=true): the model is working (a
    # slow model), NOT a swallow. The probe says "not idle" (False).
    body = b'[{"id": 0, "is_processing": true, "n_decoded_tokens": 42}]'
    _patch_urlopen(monkeypatch, result=_FakeSlotsResponse(200, body))
    assert pi_recovery.slots_idle("http://x/slots") is False


def test_slots_idle_false_when_any_of_many_slots_processing(monkeypatch):
    # One of several slots is busy: the model is working, not a swallow.
    body = b'[{"id": 0, "is_processing": false}, {"id": 1, "is_processing": true}]'
    _patch_urlopen(monkeypatch, result=_FakeSlotsResponse(200, body))
    assert pi_recovery.slots_idle("http://x/slots") is False


def test_slots_idle_none_on_network_error(monkeypatch):
    # The endpoint is unreachable (connection refused / timeout): the probe
    # is inconclusive (None) — a pure bypass, never an error.
    import urllib.error
    _patch_urlopen(
        monkeypatch, exc=urllib.error.URLError("refused"),
    )
    assert pi_recovery.slots_idle("http://x/slots") is None


def test_slots_idle_none_on_http_error(monkeypatch):
    # A non-200 (urlopen raises HTTPError): inconclusive (None).
    import urllib.error
    err = urllib.error.HTTPError(
        "http://x/slots", 500, "boom", {}, None,
    )
    _patch_urlopen(monkeypatch, exc=err)
    assert pi_recovery.slots_idle("http://x/slots") is None


def test_slots_idle_none_on_invalid_json(monkeypatch):
    # A reachable endpoint that returns non-JSON: inconclusive (None).
    _patch_urlopen(
        monkeypatch, result=_FakeSlotsResponse(200, b"not json"),
    )
    assert pi_recovery.slots_idle("http://x/slots") is None


def test_slots_idle_none_on_empty_list(monkeypatch):
    # An empty slot list is not evidence of a swallow (no slot to be
    # idle): inconclusive (None).
    _patch_urlopen(monkeypatch, result=_FakeSlotsResponse(200, b"[]"))
    assert pi_recovery.slots_idle("http://x/slots") is None


def test_slots_idle_none_on_non_list_payload(monkeypatch):
    # A JSON object (not the /slots list shape): inconclusive (None).
    _patch_urlopen(
        monkeypatch, result=_FakeSlotsResponse(200, b'{"status": "ok"}'),
    )
    assert pi_recovery.slots_idle("http://x/slots") is None


def test_slots_idle_none_on_non_dict_slot(monkeypatch):
    # A list whose entry is not a slot object: inconclusive (None).
    _patch_urlopen(monkeypatch, result=_FakeSlotsResponse(200, b'["idle"]'))
    assert pi_recovery.slots_idle("http://x/slots") is None


def test_slots_idle_uses_the_given_timeout(monkeypatch):
    # The probe is bounded (a short timeout): it never blocks the poll loop.
    calls = _patch_urlopen(
        monkeypatch,
        result=_FakeSlotsResponse(200, b'[{"id": 0, "is_processing": false}]'),
    )
    pi_recovery.slots_idle("http://x/slots", timeout=2.5)
    assert calls["timeout"] == 2.5
    assert calls["url"] == "http://x/slots"


def test_upstream_alive_false_for_live_state_without_remote_ip(
    tmp_path, monkeypatch,
):
    # A socket in a live state (SYN_SENT) whose remote address is the
    # all-zero placeholder has no remote peer yet: not a live upstream.
    proc = tmp_path / "proc"
    (proc / "42" / "fd").mkdir(parents=True)
    (proc / "42" / "stat").write_text(
        "42 (pi) S 1 " + " ".join(["0"] * 18), encoding="utf-8",
    )
    (proc / "42" / "fd" / "7").symlink_to("socket:[12345]")
    (proc / "net").mkdir()
    (proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st inode\n"
        "   0: 0100007F:1F90 00000000:0000 02 00000000:00000000 00:00000000 00000000     0        0 12345 1 00000000d7514f4a 100\n",
        encoding="utf-8",
    )
    (proc / "net" / "tcp6").write_text(
        "  sl  local_address remote_address st inode\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_recovery, "PROC", proc)
    assert pi_recovery.upstream_alive(42) is False
