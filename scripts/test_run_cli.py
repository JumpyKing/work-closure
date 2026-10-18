#!/usr/bin/env python3
"""scripts/run_cli.py 的回归测试。

只用标准库与假子进程，不调用任何真实模型 CLI。覆盖包装器四情形（同事件精确匹配、
model/provider 拆分事件、模型明确不符立即终止、无模型事件且退出 0 返回 3）、
子进程退出码透传、显示标签精确匹配、命令不存在、证据目录非空，以及进程组清理
（子进程退出后的残留后代、包装器收到终止信号）、状态优先级与 summary 写出。

运行：python3 -I -B scripts/test_run_cli.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "run_cli.py")
PY = sys.executable
with open(WRAPPER, encoding="utf-8") as _f:
    GRACE = int(re.search(r"^SIGNAL_GRACE_SECONDS = (\d+)", _f.read(), re.M).group(1))


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def pids_with(text):
    """命令行里含 text 的进程 pid（不含本进程）。"""
    res = subprocess.run(["ps", "-axo", "pid=,command="], stdout=subprocess.PIPE,
                         universal_newlines=True, errors="replace")
    if res.returncode != 0:
        raise RuntimeError("ps 失败，无法核对残留进程")
    pids = []
    for line in res.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and text in parts[1]:
            if int(parts[0]) != os.getpid():
                pids.append(int(parts[0]))
    return pids


def init_line(model):
    return json.dumps({"type": "system", "subtype": "init", "model": model})


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.counter = 0
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        # 兜底：命令行里带本测试临时目录的进程都是本测试启动的（包装器、假子进程、
        # 后代与辅助进程），断言失败或超时的路径上也一并清理；反复扫描直到没有残留，
        # 覆盖扫描之后才派生出来的后代。
        deadline = time.monotonic() + 10
        left = pids_with(self.dir)
        while left and time.monotonic() < deadline:
            for pid in left:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.05)
            left = pids_with(self.dir)
        self.tmp.cleanup()
        self.assertEqual(left, [], "测试结束时仍有残留进程")

    def child(self, body):
        """把假子进程脚本写进临时目录，返回其 argv。"""
        self.counter += 1
        path = os.path.join(self.dir, "fake_child_%d.py" % self.counter)
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(body))
        return [PY, "-I", path]

    def argv(self, child_argv, expect="M", extra=()):
        out = os.path.join(self.dir, "out_%d" % self.counter)
        argv = [PY, "-I", "-B", WRAPPER, "--label", "t", "--requested-model", "req",
                "--expect-model", expect, "--out", out]
        return argv + list(extra) + ["--"] + child_argv, out

    def run_wrapper(self, child_argv, expect="M", extra=()):
        argv, out = self.argv(child_argv, expect, extra)
        res = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return res.returncode, self.summary(out)

    def start_wrapper(self, child_argv, expect="M", extra=(), **popen_kwargs):
        argv, out = self.argv(child_argv, expect, extra)
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **popen_kwargs)
        self.procs.append(proc)
        return proc, out

    def summary(self, out):
        path = os.path.join(out, "summary.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def wait_for_pids(self, child_argv, keys, timeout=10.0):
        """等假子进程把 pid 写进 <脚本路径>.pids，返回 {键: pid}。

        包装器写 stdout.jsonl 带缓冲，运行期间读不到，所以 pid 另走旁路文件。
        """
        found = {}
        path = child_argv[-1] + ".pids"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            found.update(json.loads(line))
                        except ValueError:
                            continue
            if all(k in found for k in keys):
                return found
            time.sleep(0.05)
        self.fail("%s 在 %s 秒内没有出现 %s" % (path, timeout, keys))

    # 包装器四情形与既有退出码

    def test_default_init_exact_match_ok(self):
        child = self.child('''
            import json
            print(%r, flush=True)
            print(json.dumps({"type": "result", "result": "done"}), flush=True)
        ''' % init_line("M"))
        rc, s = self.run_wrapper(child)
        self.assertEqual(rc, 0)
        self.assertEqual(s["status"], "ok")
        self.assertTrue(s["model_match"])
        self.assertIsNone(s["signal"])
        self.assertTrue(os.path.exists(s["response_file"]))

    def test_explicit_fields_same_event_ok(self):
        child = self.child('''
            import json
            print(json.dumps({"payload": {"modelId": "M", "providerId": "P"}}), flush=True)
        ''')
        rc, s = self.run_wrapper(child, extra=[
            "--model-field", "payload.modelId", "--provider-field", "payload.providerId",
            "--expect-provider", "P"])
        self.assertEqual(rc, 0)
        self.assertEqual(s["status"], "ok")
        self.assertTrue(s["model_match"])

    def test_explicit_fields_split_events_mismatch(self):
        child = self.child('''
            import json
            print(json.dumps({"payload": {"modelId": "M"}}), flush=True)
            print(json.dumps({"payload": {"providerId": "P"}}), flush=True)
        ''')
        rc, s = self.run_wrapper(child, extra=[
            "--model-field", "payload.modelId", "--provider-field", "payload.providerId",
            "--expect-provider", "P"])
        self.assertEqual(rc, 3)
        self.assertEqual(s["status"], "model_mismatch")
        self.assertEqual(s["termination"], "incomplete_event")
        self.assertFalse(s["model_match"])

    def test_mismatch_kills_child_immediately(self):
        child = self.child('''
            import time
            print(%r, flush=True)
            time.sleep(30)
        ''' % init_line("X"))
        started = time.monotonic()
        rc, s = self.run_wrapper(child)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(rc, 3)
        self.assertEqual(s["status"], "model_mismatch")
        self.assertEqual(s["child_exit_code"], -signal.SIGKILL)
        self.assertFalse(s["model_match"])

    def test_no_model_event_exit0_returns_3(self):
        child = self.child('''
            import json
            print(json.dumps({"type": "result", "result": "hi"}), flush=True)
        ''')
        rc, s = self.run_wrapper(child)
        self.assertEqual(rc, 3)
        self.assertEqual(s["status"], "model_mismatch")
        self.assertEqual(s["termination"], "no_model_event")
        self.assertFalse(s["model_match"])

    def test_no_model_event_exit7_passthrough(self):
        child = self.child('''
            import sys
            sys.exit(7)
        ''')
        rc, s = self.run_wrapper(child)
        self.assertEqual(rc, 7)
        self.assertEqual(s["status"], "child_failed")
        self.assertEqual(s["child_exit_code"], 7)

    def test_cursor_alias_exact_only(self):
        two_spaces = "Grok 4.7  High"
        extra = ["--cursor-model-alias", two_spaces]
        ok_child = self.child('print(%r, flush=True)\n' % init_line(two_spaces))
        rc, s = self.run_wrapper(ok_child, expect="Grok 4.7 256K High", extra=extra)
        self.assertEqual((rc, s["status"]), (0, "ok"))
        near_child = self.child('print(%r, flush=True)\n' % init_line("Grok 4.7 High"))
        rc, s = self.run_wrapper(near_child, expect="Grok 4.7 256K High", extra=extra)
        self.assertEqual((rc, s["status"]), (3, "model_mismatch"))

    def test_command_not_found_127(self):
        rc, s = self.run_wrapper([os.path.join(self.dir, "no-such-cli")])
        self.assertEqual(rc, 127)
        self.assertEqual(s["status"], "capability_unavailable")

    def test_command_not_executable_126(self):
        path = os.path.join(self.dir, "not-executable")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o644)
        rc, s = self.run_wrapper([path])
        self.assertEqual(rc, 126)
        self.assertEqual(s["status"], "capability_unavailable")

    def test_evidence_dir_not_empty_returns_2(self):
        argv, out = self.argv([PY, "-c", "pass"])
        os.makedirs(out)
        with open(os.path.join(out, "keep.txt"), "w") as f:
            f.write("x")
        res = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(res.returncode, 2)
        self.assertIn("evidence_dir_not_empty", res.stdout.decode("utf-8"))
        self.assertIsNone(self.summary(out))

    def test_child_killed_by_signal_maps_exit_code(self):
        child = self.child('''
            import os, signal
            print(%r, flush=True)
            os.kill(os.getpid(), signal.SIGTERM)
        ''' % init_line("M"))
        rc, s = self.run_wrapper(child)
        self.assertEqual(s["status"], "child_failed")
        self.assertEqual(s["child_exit_code"], -signal.SIGTERM)
        self.assertEqual(rc, 128 + signal.SIGTERM)
        self.assertEqual(s["exit_code"], 128 + signal.SIGTERM)

    def test_closed_wrapper_stdout_keeps_exit_code(self):
        # 调用方已断开包装器的 stdout：summary 照常落盘，退出码以 summary 为准，不会变成 1 或 120。
        child = self.child('''
            import time
            print(%r, flush=True)
            time.sleep(1)
        ''' % init_line("M"))
        argv, out = self.argv(child)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        proc.stdout.close()
        proc.wait(timeout=30)
        s = self.summary(out)
        self.assertEqual((proc.returncode, s["status"], s["exit_code"]), (0, "ok", 0))

    # 子进程退出后的进程组清理

    def test_waits_for_child_that_closed_stdout(self):
        # stdout 关闭后子进程仍在收尾：包装器应等它自己退出（不回收）、拿到真实退出码，
        # 不能把它当成已退出而提前清理。
        child = self.child('''
            import os, sys, time
            print(%r, flush=True)
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
            time.sleep(1.5)
            sys.exit(5)
        ''' % init_line("M"))
        started = time.monotonic()
        rc, s = self.run_wrapper(child)
        elapsed = time.monotonic() - started
        self.assertEqual((rc, s["status"], s["child_exit_code"]), (5, "child_failed", 5))
        self.assertGreater(elapsed, 1.2)
        self.assertLess(elapsed, 8)

    def test_stderr_only_descendant_cleaned_after_leader_exit(self):
        child = self.child('''
            import json, os, subprocess, sys, time
            pids = os.path.abspath(__file__) + ".pids"
            subprocess.Popen([sys.executable, "-I", "-c",
                "import json, os, sys, time\\n"
                "open(sys.argv[1], 'a').write(json.dumps({'descendant': os.getpid()}) + '\\\\n')\\n"
                "time.sleep(60)", pids], stdout=subprocess.DEVNULL)
            while not (os.path.exists(pids) and "descendant" in open(pids).read()):
                time.sleep(0.01)
            print(json.dumps({"type": "system", "subtype": "init", "model": "M"}), flush=True)
        ''')
        started = time.monotonic()
        rc, s = self.run_wrapper(child)
        elapsed = time.monotonic() - started
        descendant = self.wait_for_pids(child, ["descendant"])["descendant"]
        self.assertEqual((rc, s["status"], s["child_exit_code"]), (0, "ok", 0))
        self.assertTrue(wait_gone(descendant), "子进程退出后组内只占着 stderr 的后代应被清理")
        self.assertLess(elapsed, 8, "不应等满 stderr 的 10 秒超时")

    # 包装器自身收到终止信号

    def sleeper_child(self):
        return self.child('''
            import json, os, time
            print(json.dumps({"type": "system", "subtype": "init", "model": "M"}), flush=True)
            with open(os.path.abspath(__file__) + ".pids", "a") as f:
                f.write(json.dumps({"pid": os.getpid()}) + "\\n")
            time.sleep(60)
        ''')

    def test_sigterm_during_blocked_read(self):
        bystander = subprocess.Popen([PY, "-I", "-c", "import time; time.sleep(60)", self.dir])
        self.procs.append(bystander)
        child = self.sleeper_child()
        proc, out = self.start_wrapper(child)
        child_pid = self.wait_for_pids(child, ["pid"])["pid"]
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
        s = self.summary(out)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
        self.assertEqual(s["status"], "terminated")
        self.assertEqual(s["signal"], "SIGTERM")
        self.assertEqual(s["termination"], "signal_SIGTERM")
        self.assertTrue(s["model_match"])
        self.assertEqual(s["child_exit_code"], -signal.SIGKILL)
        self.assertTrue(wait_gone(child_pid))
        self.assertIsNone(bystander.poll(), "无关进程不应被清理")

    def test_sigterm_kills_descendants_after_leader_exit(self):
        child = self.child('''
            import json, os, subprocess, sys
            pids = os.path.abspath(__file__) + ".pids"
            subprocess.Popen([sys.executable, "-I", "-c",
                "import json, os, sys, time\\n"
                "open(sys.argv[1], 'a').write(json.dumps({'grandchild': os.getpid()}) + '\\\\n')\\n"
                "time.sleep(60)", pids])
            print(json.dumps({"type": "system", "subtype": "init", "model": "M"}), flush=True)
            with open(pids, "a") as f:
                f.write(json.dumps({"pid": os.getpid()}) + "\\n")
        ''')
        proc, out = self.start_wrapper(child)
        found = self.wait_for_pids(child, ["pid", "grandchild"])
        time.sleep(0.5)  # 组长已退出，孙进程仍持有 stdout，包装器还在读
        self.assertIsNone(proc.poll())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
        s = self.summary(out)
        self.assertEqual(s["status"], "terminated")
        self.assertTrue(wait_gone(found["grandchild"]), "同一进程组的后代应被清理")

    def test_repeated_signals_still_write_summary(self):
        child = self.sleeper_child()
        proc, out = self.start_wrapper(child)
        child_pid = self.wait_for_pids(child, ["pid"])["pid"]
        for sig in (signal.SIGTERM, signal.SIGTERM, signal.SIGINT):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                break
        proc.wait(timeout=15)
        s = self.summary(out)
        self.assertEqual(s["status"], "terminated")
        # 同时待处理的多个信号，CPython 按信号编号从小到大调用处理函数，
        # 所以记下的"第一个"可能是 SIGINT；只要求属于发出的信号且退出码一致。
        self.assertIn(s["signal"], ("SIGTERM", "SIGINT"))
        self.assertEqual(proc.returncode, 128 + signal.Signals[s["signal"]].value)
        self.assertTrue(wait_gone(child_pid))

    def test_simultaneous_signals_leave_no_traceback(self):
        # 两个不同的终止信号同时待分发：后一个应被屏蔽或直接返回，不能让解释器打印
        # "ignored due to race condition" 之类的异常。
        child = self.sleeper_child()
        argv, out = self.argv(child)
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                universal_newlines=True)
        self.procs.append(proc)
        self.wait_for_pids(child, ["pid"])
        proc.send_signal(signal.SIGTERM)
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=30)
        s = self.summary(out)
        self.assertEqual(s["status"], "terminated")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("race condition", err)

    def test_ignored_sighup_stays_ignored(self):
        child = self.sleeper_child()
        proc, out = self.start_wrapper(
            child, preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN))
        child_pid = self.wait_for_pids(child, ["pid"])["pid"]
        proc.send_signal(signal.SIGHUP)
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), "启动时被忽略的 SIGHUP 应继续忽略")
        self.assertTrue(alive(child_pid))
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
        s = self.summary(out)
        self.assertEqual(s["status"], "terminated")
        self.assertEqual(s["signal"], "SIGTERM")

    def test_sighup_and_sigint_exit_codes(self):
        reset = lambda: (signal.signal(signal.SIGHUP, signal.SIG_DFL),
                         signal.signal(signal.SIGINT, signal.SIG_DFL))
        for sig in (signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig=sig.name):
                child = self.sleeper_child()
                proc, out = self.start_wrapper(child, preexec_fn=reset)
                child_pid = self.wait_for_pids(child, ["pid"])["pid"]
                proc.send_signal(sig)
                proc.wait(timeout=15)
                s = self.summary(out)
                self.assertEqual((proc.returncode, s["status"], s["signal"]),
                                 (128 + sig, "terminated", sig.name))
                self.assertTrue(wait_gone(child_pid))

    def test_other_session_pipe_holder_exits_after_grace(self):
        # 另开会话的后代占着 stdout：收到信号后不能无限等 EOF，宽限期后照常写 summary 退出。
        child = self.child('''
            import json, os, subprocess, sys, time
            pids = os.path.abspath(__file__) + ".pids"
            subprocess.Popen([sys.executable, "-I", "-c",
                "import json, os, sys, time\\n"
                "open(sys.argv[1], 'a').write(json.dumps({'holder': os.getpid()}) + '\\\\n')\\n"
                "time.sleep(60)", pids], start_new_session=True)
            print(json.dumps({"type": "system", "subtype": "init", "model": "M"}), flush=True)
            with open(pids, "a") as f:
                f.write(json.dumps({"pid": os.getpid()}) + "\\n")
            time.sleep(60)
        ''')
        proc, out = self.start_wrapper(child)
        found = self.wait_for_pids(child, ["pid", "holder"])
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        elapsed = time.monotonic() - started
        s = self.summary(out)
        self.assertEqual((proc.returncode, s["status"], s["signal"]),
                         (128 + signal.SIGTERM, "terminated", "SIGTERM"))
        self.assertIn("管道仍未关闭", s["error"])
        self.assertLess(elapsed, 15, "宽限期后应退出，不应无限等待")
        self.assertTrue(wait_gone(found["pid"]), "子进程本身应被清理")
        self.assertTrue(alive(found["holder"]), "另开会话的后代不在清理保证内（文档已说明）")

    def test_signal_while_popen_returns_kills_child(self):
        # 确定性地造出启动窗口：子进程已启动、Popen 尚未把对象交给包装器时收到 SIGTERM。
        child = self.sleeper_child()
        argv, out = self.argv(child)
        launcher = (
            "import os, runpy, signal, subprocess, sys\n"
            "class Popen(subprocess.Popen):\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        super().__init__(*args, **kwargs)\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "subprocess.Popen = Popen\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        started = time.monotonic()
        res = subprocess.run([PY, "-I", "-B", "-c", launcher] + argv[3:],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        elapsed = time.monotonic() - started
        s = self.summary(out)
        self.assertEqual((res.returncode, s["status"], s["signal"]),
                         (128 + signal.SIGTERM, "terminated", "SIGTERM"))
        self.assertLess(elapsed, 4, "启动窗口内收到的信号应立即清理子进程，不应等到宽限期")
        deadline = time.monotonic() + 5
        left = pids_with(child[-1])
        while left and time.monotonic() < deadline:
            time.sleep(0.05)
            left = pids_with(child[-1])
        self.assertEqual(left, [], "不应残留子进程")

    def test_grace_expired_before_reading_does_not_hang(self):
        # 启动窗口收到信号、Popen 过了宽限期才返回，另开会话的后代占着 stdout：
        # 进入读取时按截止时间判断已到期，直接收尾，不能无限等待。
        child = self.child('''
            import json, os, subprocess, sys, time
            subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)",
                              os.path.abspath(__file__)], start_new_session=True)
            print(json.dumps({"type": "system", "subtype": "init", "model": "M"}), flush=True)
            time.sleep(60)
        ''')
        argv, out = self.argv(child)
        launcher = (
            "import os, runpy, signal, subprocess, sys, time\n"
            "class Popen(subprocess.Popen):\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        super().__init__(*args, **kwargs)\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "        time.sleep(%d)\n"
            "subprocess.Popen = Popen\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n") % (GRACE + 1)
        started = time.monotonic()
        res = subprocess.run([PY, "-I", "-B", "-c", launcher] + argv[3:],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
        elapsed = time.monotonic() - started
        s = self.summary(out)
        self.assertEqual((res.returncode, s["status"]), (128 + signal.SIGTERM, "terminated"))
        self.assertIn("stdout 管道", s["error"])
        self.assertLess(elapsed, GRACE + 6)

    def test_signal_during_stderr_wait_respects_grace(self):
        # 子进程已退出、包装器在等 stderr（另开会话的后代占着它）时收到信号：
        # stderr 与 stdout 共用宽限截止时间，不能再按 10 秒等。
        child = self.child('''
            import json, os, subprocess, sys, time
            pids = os.path.abspath(__file__) + ".pids"
            subprocess.Popen([sys.executable, "-I", "-c",
                "import json, os, sys, time\\n"
                "open(sys.argv[1], 'a').write(json.dumps({'holder': os.getpid()}) + '\\\\n')\\n"
                "time.sleep(60)", pids], start_new_session=True, stdout=subprocess.DEVNULL)
            while not (os.path.exists(pids) and "holder" in open(pids).read()):
                time.sleep(0.01)
            print(json.dumps({"type": "system", "subtype": "init", "model": "M"}), flush=True)
        ''')
        proc, out = self.start_wrapper(child)
        self.wait_for_pids(child, ["holder"])
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), "包装器应仍在等 stderr")
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
        elapsed = time.monotonic() - started
        s = self.summary(out)
        self.assertEqual((proc.returncode, s["status"]), (128 + signal.SIGTERM, "terminated"))
        self.assertIn("stderr 管道", s["error"])
        self.assertLess(elapsed, GRACE + 2, "收到信号后等 stderr 不应超过宽限期")

    def test_ignored_sigchld_keeps_real_exit_code(self):
        # SIGCHLD 被忽略时子进程会被内核自动回收、退出码丢失；包装器启动时恢复默认。
        # Linux 上跨 exec 继承的忽略即会生效，macOS 上要在本进程内设置才生效，
        # 所以这里在包装器进程内先设成忽略，再运行包装器，两种平台都能复现。
        child = self.child('''
            import sys
            print(%r, flush=True)
            sys.exit(7)
        ''' % init_line("M"))
        argv, out = self.argv(child)
        launcher = (
            "import runpy, signal, sys\n"
            "signal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        res = subprocess.run([PY, "-I", "-B", "-c", launcher] + argv[3:],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        s = self.summary(out)
        self.assertEqual((res.returncode, s["status"], s["child_exit_code"]), (7, "child_failed", 7))

    def test_signal_before_mismatch_keeps_mismatch(self):
        # 顺序固定为"先处理信号、后判定模型不符"：辅助进程另开会话，等子进程被
        # 包装器的信号清理杀掉之后，才把错误模型事件写进同一个 stdout 管道。
        child = self.child('''
            import os, signal, subprocess, sys, time
            ready = os.path.abspath(__file__) + ".ready"
            helper = (
                "import json, os, sys, time\\n"
                "child = os.getppid()\\n"
                "open(sys.argv[1], 'w').close()\\n"
                "while os.getppid() == child:\\n"
                "    time.sleep(0.01)\\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'X'}), flush=True)\\n")
            subprocess.Popen([sys.executable, "-I", "-c", helper, ready], start_new_session=True)
            while not os.path.exists(ready):
                time.sleep(0.01)
            os.kill(os.getppid(), signal.SIGTERM)
            time.sleep(30)
        ''')
        rc, s = self.run_wrapper(child)
        self.assertEqual((rc, s["status"], s["signal"]), (3, "model_mismatch", "SIGTERM"))
        self.assertEqual(s["exit_code"], 3)
        self.assertFalse(s["model_match"])

    def test_mismatch_before_signal_keeps_mismatch(self):
        # 顺序固定为"先判定模型不符、后收到信号"：辅助进程另开会话并占着 stderr，
        # 等子进程因模型不符被杀掉之后才向包装器发 SIGTERM，此时包装器正在等 stderr 收尾。
        child = self.child('''
            import json, os, signal, subprocess, sys, time
            ready = os.path.abspath(__file__) + ".ready"
            helper = (
                "import os, signal, sys, time\\n"
                "child = os.getppid()\\n"
                "wrapper = int(sys.argv[2])\\n"
                "open(sys.argv[1], 'w').close()\\n"
                "while os.getppid() == child:\\n"
                "    time.sleep(0.01)\\n"
                "os.kill(wrapper, signal.SIGTERM)\\n"
                "time.sleep(0.5)\\n")
            subprocess.Popen([sys.executable, "-I", "-c", helper, ready, str(os.getppid())],
                             start_new_session=True, stdout=subprocess.DEVNULL)
            while not os.path.exists(ready):
                time.sleep(0.01)
            print(json.dumps({"type": "system", "subtype": "init", "model": "X"}), flush=True)
            time.sleep(30)
        ''')
        rc, s = self.run_wrapper(child)
        self.assertEqual((rc, s["status"], s["signal"]), (3, "model_mismatch", "SIGTERM"))
        self.assertEqual(s["exit_code"], 3)
        self.assertFalse(s["model_match"])

    def test_signal_during_startup_leaves_no_child(self):
        for delay in (0.0, 0.01, 0.03, 0.06, 0.1, 0.2, 0.35, 0.5):
            with self.subTest(delay=delay):
                child = self.sleeper_child()
                marker = child[-1]
                proc, out = self.start_wrapper(child)
                time.sleep(delay)
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=15)
                s = self.summary(out)
                if s is None:
                    # 信号早于处理函数安装：包装器按默认动作退出，尚未启动子进程。
                    self.assertEqual(proc.returncode, -signal.SIGTERM)
                else:
                    self.assertEqual(s["status"], "terminated")
                    self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
                deadline = time.monotonic() + 5
                left = pids_with(marker)
                while left and time.monotonic() < deadline:
                    time.sleep(0.05)
                    left = pids_with(marker)
                self.assertEqual(left, [], "不应残留子进程")


if __name__ == "__main__":
    unittest.main(verbosity=2)
