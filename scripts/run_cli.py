#!/usr/bin/env python3
"""work-closure 唯一 CLI 包装器。

包装 cursor-agent 或 ZCode 的子进程调用：把 stdout 与 stderr 原样落盘，
逐行解析 stream-json 事件确认实际 provider/model，另存真实计划与响应文本，
最后只向本进程 stdout 打印一条紧凑 JSON summary。仅依赖 Python 标准库。

summary 不记录完整 command、prompt、环境或用户任务文本，只记录 executable、
参数数量与参数数组的 SHA256，长输入也保持紧凑。

包装器自身收到 SIGTERM、SIGHUP 或 SIGINT 时终止子进程所在的进程组，
仍写出 summary（status 为 terminated），避免留下无人管理的外部模型进程。
子进程退出后、回收之前，包装器同样终止它所在的进程组，清掉残留的后代。
"""

import argparse
import errno
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

_MISSING = object()

# 包装器处理的终止信号。
TERM_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
# 收到终止信号后最多再等这么久让管道关闭：另开会话的后代可能仍占着管道，
# 不能无限等 EOF，超时就停止读取，照常回收子进程并写 summary。
SIGNAL_GRACE_SECONDS = 5


class _GraceExpired(Exception):
    """收到终止信号后，管道在宽限期内仍未关闭。"""


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def shell_exit_code(code):
    """子进程被信号终止时 returncode 是负的信号值；换成 shell 约定的 128 加信号值。

    直接 sys.exit(负数) 在外部会变成 256 减信号值（例如 -15 变成 241），
    调用方无法看出是信号终止。
    """
    if code is not None and code < 0:
        return 128 - code
    return code


def wait_exit_unreaped(pid, timeout):
    """等子进程退出但不回收它：返回 True（已退出）、False（超时仍在运行）或 None（身份丢失）。

    子进程回收之前它的 pid（也就是它的进程组号）不会被复用，之后对该进程组的
    清理一定落在本组上。Linux 用 waitid(WNOWAIT)，macOS 等 BSD 用 kqueue 的 NOTE_EXIT。
    查不到该子进程（已被别处回收）时返回 None，调用方不得再按旧组号清理。
    """
    if hasattr(os, "waitid"):
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
                    return True
            except ChildProcessError:
                return None
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
    try:
        kq = select.kqueue()
    except OSError:
        return False
    try:
        event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              fflags=select.KQ_NOTE_EXIT)
        events = kq.control([event], 1, timeout)
        if not events:
            return False
        if events[0].flags & select.KQ_EV_ERROR:
            # 注册失败以错误事件返回而不是抛异常。macOS 对已退出、尚未回收的僵尸进程
            # 同样返回 ESRCH：此时对它发信号 0 仍会成功（还占着进程表项），即已退出；
            # 发不出去说明它已被别处回收，身份丢失。其他错误视为未知。
            if events[0].data != errno.ESRCH:
                return False
            try:
                os.kill(pid, 0)
            except OSError:
                return None
            return True
        return True
    except ProcessLookupError:
        return None
    except OSError:
        return False
    finally:
        kq.close()


def text_of(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def get_path(obj, dotted):
    """按点分路径取值，返回 (路径是否存在, 值)。"""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def find_plan(obj):
    """递归查找 createPlanToolCall 携带的真实计划文本。"""
    if isinstance(obj, dict):
        node = obj.get("createPlanToolCall")
        if isinstance(node, dict):
            for holder in (node.get("args"), node):
                if isinstance(holder, dict):
                    plan = holder.get("plan")
                    if isinstance(plan, str) and plan.strip():
                        return plan
        if obj.get("name") == "createPlanToolCall" and isinstance(obj.get("args"), dict):
            plan = obj["args"].get("plan")
            if isinstance(plan, str) and plan.strip():
                return plan
        for value in obj.values():
            found = find_plan(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_plan(value)
            if found is not None:
                return found
    return None


def find_response(obj):
    """递归查找 result.result 字符串（最终响应文本，可能只是计划的前言）。"""
    if isinstance(obj, dict):
        inner = obj.get("result")
        if isinstance(inner, dict):
            text = inner.get("result")
            if isinstance(text, str) and text.strip():
                return text
        for value in obj.values():
            found = find_response(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_response(value)
            if found is not None:
                return found
    return None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run_cli.py",
        description="work-closure 唯一 CLI 包装器：调用 cursor-agent 或 ZCode，"
        "落盘原始证据并校验实际 provider/model，只打印紧凑 JSON summary。",
        epilog="参数说明：--label 为本次调用标签；--requested-model 为该角色期望的"
        "模型 ID；--expect-model 为实际模型事件必须匹配的期望文本；--model-field 与 "
        "--provider-field 为点分事件路径，二者必须取自同一事件，显式给出时按精确相等"
        "比较；--expect-provider 为实际 provider 事件必须精确相等的期望文本；"
        "--cursor-model-alias 为 cursor 显示标签的已核实附加完整标签，可重复，仅与 "
        "--expect-model 共同组成有限允许集合，逐字精确相等，不开放给显式 ZCode 字段；"
        "--out 为本次全新的证据目录。-- 之后跟被调用程序的完整参数数组。",
    )
    parser.add_argument("--label", required=True,
                        help="本次调用标签，写入 summary.json 的 label 字段")
    parser.add_argument("--requested-model", required=True,
                        help="该角色期望使用的模型 ID，原样写入 summary.json 的 requested_model 字段")
    parser.add_argument("--expect-model", required=True,
                        help="实际模型事件值必须精确相等的完整期望文本；带任何额外前后缀或"
                        "变体（如 Low 档）均判不符")
    parser.add_argument("--cursor-model-alias", action="append", default=[],
                        help="cursor 显示标签的已核实附加完整标签，可重复；与 --expect-model "
                        "组成有限允许集合，仅接受逐字精确相等，不做子串、前缀、宽松正则或"
                        "自动清洗；不给时行为不变。禁止与 --model-field/--provider-field 同用")
    parser.add_argument("--model-field", default=None,
                        help="模型字段的点分事件路径，例如 payload.modelId；缺省只认 type=system 且 "
                        "subtype=init 事件里的 model 字段，这是 cursor-agent 的默认取法")
    parser.add_argument("--expect-provider", default=None,
                        help="实际 provider 事件值必须精确相等的期望文本；不给则不校验 provider")
    parser.add_argument("--provider-field", default=None,
                        help="provider 字段的点分事件路径，例如 payload.providerId；必须与 "
                        "--model-field 取自同一事件")
    parser.add_argument("--out", required=True,
                        help="本次全新的证据目录；已存在且含文件时拒绝覆盖。写入 stdout.jsonl、"
                        "stderr.txt、summary.json，出现计划或响应文本时另存 plan.md 与 response.md")
    return parser


def main():
    raw = sys.argv
    if "--" in raw:
        sep = raw.index("--")
        head, child_argv = raw[1:sep], raw[sep + 1:]
    else:
        head, child_argv = raw[1:], []
    parser = build_parser()
    args = parser.parse_args(head)
    if not child_argv:
        parser.error("-- 之后必须跟被调用程序的完整参数数组")
    if (args.model_field is None) != (args.provider_field is None):
        parser.error("--model-field 与 --provider-field 必须同时给出或同时省略")
    if args.cursor_model_alias and args.model_field is not None:
        parser.error("--cursor-model-alias 不允许与 --model-field/--provider-field 同用："
                     "显式 ZCode 字段只接受与 --expect-model 精确相等")

    # 显示标签的有限允许集合：仅 expect-model 加显式 alias，逐字精确相等，
    # 无自动清洗或扩展。
    accepted_cursor_models = None
    if args.model_field is None:
        accepted_cursor_models = [args.expect_model]
        for alias in args.cursor_model_alias:
            if alias not in accepted_cursor_models:
                accepted_cursor_models.append(alias)

    out_dir = args.out
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        print(json.dumps({
            "status": "evidence_dir_not_empty",
            "error": "证据目录已存在且含文件，拒绝覆盖: " + out_dir,
        }, ensure_ascii=False, separators=(",", ":")))
        return 2

    os.makedirs(out_dir, exist_ok=True)
    stdout_path = os.path.join(out_dir, "stdout.jsonl")
    stderr_path = os.path.join(out_dir, "stderr.txt")
    summary_path = os.path.join(out_dir, "summary.json")

    started_at = datetime.now(timezone.utc).isoformat()
    start_mono = time.monotonic()

    explicit_fields = args.model_field is not None
    models_seen = []
    providers_seen = []
    model_event_count = 0
    provider_event_count = 0
    incomplete_events = 0
    observed_model = None
    observed_provider = None
    usage = "unavailable"
    plan_text = None
    response_text = None
    json_lines = 0
    non_json_lines = 0
    status = None
    exit_code = None
    child_exit_code = None
    model_ok = None
    error = None
    termination = None
    proc = None
    err_thread = None
    # 包装器自身收到的第一个终止信号与宽限截止时间（单调时钟）；
    # 子进程回收之后收到的信号不再记录。
    received = {"signum": None, "deadline": None, "child_reaped": False}
    # 主线程是否正在读子进程 stdout；宽限期到时只在读取期间打断读取。
    reading = {"active": False}
    # 收尾时停止等待管道等情况的说明，最后并入 summary 的 error。
    notes = []

    def kill_child_group():
        """终止子进程所在的进程组（含被包装 CLI 的后代）。

        start_new_session=True 让子进程自成会话与进程组，组号等于子进程 pid。
        只在子进程回收之前调用（回收前组号不会被复用），只覆盖这个进程组，
        不碰包装器自己所在的组；后代若另开会话则不在保证内。
        """
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    def on_signal(signum, frame):
        # 不在处理函数里抛异常：清理进程组后读循环会因管道 EOF 自然结束，
        # 后续照常回收子进程并写 summary。重复信号、子进程回收之后的信号都忽略。
        if received["signum"] is not None or received["child_reaped"]:
            return
        if proc is not None and proc.returncode is not None:
            return
        received["signum"] = signum
        received["deadline"] = time.monotonic() + SIGNAL_GRACE_SECONDS
        # 之后的终止信号一律屏蔽，保证收尾与 summary 写完。用屏蔽而不是改成忽略：
        # 已经同时到达、待分发的另一个终止信号仍会走到本函数并直接返回，
        # 改成忽略会让解释器打印"ignored due to race condition"。
        signal.pthread_sigmask(signal.SIG_BLOCK, TERM_SIGNALS)
        if proc is not None:
            kill_child_group()
        signal.setitimer(signal.ITIMER_REAL, SIGNAL_GRACE_SECONDS)

    def on_alarm(signum, frame):
        # 宽限期到：只在主线程仍在读 stdout 时打断读取，其他阶段不受影响；
        # 读取开始前就已到期的情形由进入读取时的截止时间检查处理。
        if reading["active"]:
            raise _GraceExpired()

    def grace_left():
        """收到过终止信号时返回距宽限截止的秒数，否则返回 None。"""
        if received["deadline"] is None:
            return None
        return received["deadline"] - time.monotonic()

    def wait_stderr(seconds):
        """等 stderr 读线程收尾，最多 seconds 秒；收到过终止信号时不超过宽限截止时间。"""
        end = time.monotonic() + seconds
        while err_thread.is_alive():
            left = end - time.monotonic()
            g = grace_left()
            if g is not None:
                left = min(left, g)
            if left <= 0:
                break
            err_thread.join(min(left, 0.1))
        if err_thread.is_alive():
            notes.append("stderr 管道在时限内仍未关闭（可能有另开会话的后代占着管道），已停止等待")

    for sig in TERM_SIGNALS:
        # 启动时已被忽略的信号保持忽略，例如 nohup 下的 SIGHUP、
        # 非交互 shell 后台任务的 SIGINT。
        if signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, on_signal)
    signal.signal(signal.SIGALRM, on_alarm)
    # 子进程退出后必须留到包装器回收：继承来的 SIGCHLD 忽略会让内核自动回收子进程，
    # 退出码丢失（Popen 会记成 0），组号也可能被复用。
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)

    def dedup_append(lst, text):
        if text not in lst:
            lst.append(text)

    def model_matches(value):
        # 统一精确相等：cursor 显示标签必须逐字等于有限允许集合（expect-model
        # 加显式 alias）中的某一完整标签；带额外前后缀或 Low 变体一律判不符。
        # 显式 ZCode 取法仍仅与 --expect-model 精确相等，不受 alias 影响。
        if accepted_cursor_models is not None:
            return value in accepted_cursor_models
        return value == args.expect_model

    def handle_event(event):
        """返回 None，或返回 (失败原因)；失败即模型明确不符或不完整。"""
        nonlocal observed_model, observed_provider, usage, plan_text, response_text
        nonlocal model_event_count, provider_event_count, incomplete_events
        model_val = _MISSING
        provider_val = _MISSING
        if explicit_fields:
            model_ok_path, model_val = get_path(event, args.model_field)
            provider_ok_path, provider_val = get_path(event, args.provider_field)
            if model_ok_path != provider_ok_path:
                # 同一事件只出现 model 或 provider 之一：不完整事件，不可作为通过依据。
                incomplete_events += 1
                return "incomplete_event"
            if not model_ok_path:
                pass
            else:
                model_event_count += 1
                provider_event_count += 1
                model_text = text_of(model_val)
                provider_text = text_of(provider_val)
                dedup_append(models_seen, model_text)
                dedup_append(providers_seen, provider_text)
                if observed_model is None:
                    observed_model = model_text
                if observed_provider is None:
                    observed_provider = provider_text
                if not model_matches(model_text):
                    return "model_mismatch"
                if args.expect_provider is not None and provider_text != args.expect_provider:
                    return "provider_mismatch"
        elif event.get("type") == "system" and event.get("subtype") == "init" and "model" in event:
            model_text = text_of(event["model"])
            model_event_count += 1
            dedup_append(models_seen, model_text)
            if observed_model is None:
                observed_model = model_text
            if not model_matches(model_text):
                return "model_mismatch"
        if usage == "unavailable" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if plan_text is None:
            plan_text = find_plan(event)
        if event.get("type") == "result" and isinstance(event.get("result"), str) \
                and event["result"].strip():
            response_text = event["result"]
        else:
            found = find_response(event)
            if found is not None:
                response_text = found
        return None

    out_f = open(stdout_path, "wb")
    err_f = open(stderr_path, "wb")
    try:
        try:
            # 子进程自成会话与进程组：终止时只覆盖这个进程组，
            # 不影响外层 PM2 父进程、包装器自己所在的组或其他任务。
            proc = subprocess.Popen(child_argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    start_new_session=True)
        except FileNotFoundError:
            status = "capability_unavailable"
            exit_code = 127
            error = "command not found: " + child_argv[0]
        except PermissionError:
            status = "capability_unavailable"
            exit_code = 126
            error = "command not executable: " + child_argv[0]
        else:
            if received["signum"] is not None:
                # Popen 返回之前已收到信号：子进程刚启动，立即补做清理。
                kill_child_group()

            def drain_stderr():
                try:
                    while True:
                        chunk = proc.stderr.read(65536)
                        if not chunk:
                            break
                        try:
                            err_f.write(chunk)
                        except (ValueError, OSError):
                            # 宽限期后主线程已关闭证据文件，读线程不再写。
                            break
                finally:
                    try:
                        proc.stderr.close()
                    except OSError:
                        pass

            # 读线程在创建时继承屏蔽集，终止信号与宽限计时因此只投递给主线程，
            # 不会落到阻塞在 stderr 上的读线程、让主线程迟迟不处理。
            thread_sigs = set(TERM_SIGNALS) | {signal.SIGALRM}
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, thread_sigs)
            try:
                err_thread = threading.Thread(target=drain_stderr, daemon=True)
                err_thread.start()
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            stderr_waited = False
            try:
                try:
                    reading["active"] = True
                    left = grace_left()
                    if left is not None:
                        # 读取开始前已收到信号：宽限计时可能已在读取前到期而作废，
                        # 按截止时间重新判断，只给剩余的宽限。
                        if left <= 0:
                            raise _GraceExpired()
                        signal.setitimer(signal.ITIMER_REAL, left)
                    for raw_line in iter(proc.stdout.readline, b""):
                        out_f.write(raw_line)
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        try:
                            event = json.loads(stripped.decode("utf-8", "replace"))
                        except ValueError:
                            non_json_lines += 1
                            continue
                        json_lines += 1
                        if isinstance(event, dict):
                            failure = handle_event(event)
                            if failure is not None and status is None:
                                # 实际模型事件明确不符：立即终止子进程所在的
                                # 进程组，停止后续工具副作用，不再消费剩余输出。
                                status = "model_mismatch"
                                termination = failure
                                exit_code = 3
                                kill_child_group()
                                break
                    reading["active"] = False
                except _GraceExpired:
                    reading["active"] = False
                    notes.append("收到终止信号后 %d 秒内 stdout 管道仍未关闭（可能有另开会话的"
                                 "后代占着管道），已停止读取" % SIGNAL_GRACE_SECONDS)
                signal.setitimer(signal.ITIMER_REAL, 0)
                # 收尾顺序：等子进程退出但不回收 → 终止它所在的整个进程组 → 等 stderr
                # 收尾 → 回收子进程。子进程回收之前组号不会被复用，所以这里的清理一定
                # 落在本组上，也能清掉子进程退出后仍留在组内的后代（例如只占着 stderr
                # 的后代）；收尾期间收到的信号同样能安全清理。子进程已退出时清理不改变
                # 它的真实退出码；10 秒内仍未退出才会被这次清理终止。
                try:
                    proc.stdout.close()
                except OSError:
                    pass
                if wait_exit_unreaped(proc.pid, 10) is None:
                    notes.append("子进程已被别处回收，身份无法确认，未清理进程组")
                else:
                    kill_child_group()
                # stderr 与 stdout 共用宽限截止时间：收到过终止信号时不超过它，否则最多 10 秒。
                wait_stderr(10)
                stderr_waited = True
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                child_exit_code = proc.returncode
            finally:
                # 意外异常路径上也先撤掉宽限计时，免得它之后在别处打断。
                reading["active"] = False
                signal.setitimer(signal.ITIMER_REAL, 0)
                if proc.returncode is None:
                    # 异常路径：子进程尚未回收，组号仍属于本组，可以安全清理。
                    kill_child_group()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                if proc.returncode is not None:
                    received["child_reaped"] = True
                if not stderr_waited and err_thread is not None and err_thread.is_alive():
                    wait_stderr(2)
    finally:
        out_f.close()
        err_f.close()

    if proc is not None and child_exit_code is None:
        # 收尾里的 wait 超时、异常路径上才回收到的子进程：补读真实退出码。
        child_exit_code = proc.returncode
        if child_exit_code is None:
            notes.append("子进程在清理后仍未退出，未能取得退出码")

    signum = received["signum"]
    if status is None and signum is not None:
        # 子进程运行期间包装器收到终止信号。模型不符已先行判定时保留 model_mismatch，
        # 命令不可用时保留 capability_unavailable。
        status = "terminated"
        termination = "signal_" + signal.Signals(signum).name
        exit_code = 128 + signum

    if status is None:
        if observed_model is None:
            # 没有任何模型事件：模型未验证。子进程真实非零退出码优先保留，
            # 不被 no_model_event 覆盖；仅当子进程本身正常退出时才按
            # 模型校验失败返回 3。缺模型经 termination 与 model_match 标明。
            termination = "no_model_event"
            if child_exit_code is not None and child_exit_code != 0:
                status = "child_failed"
                exit_code = shell_exit_code(child_exit_code)
            else:
                status = "model_mismatch"
                exit_code = 3
        elif incomplete_events:
            status = "model_mismatch"
            termination = "incomplete_event"
            exit_code = 3
        else:
            model_ok = all(model_matches(m) for m in models_seen)
            if args.expect_provider is not None:
                model_ok = model_ok and observed_provider == args.expect_provider
            if not model_ok:
                status = "model_mismatch"
                termination = "aggregated_mismatch"
                exit_code = 3
            else:
                status = "ok" if child_exit_code == 0 else "child_failed"
                # 取不到子进程退出码时返回 1，不能让 sys.exit(None) 变成成功的 0。
                exit_code = shell_exit_code(child_exit_code) if child_exit_code is not None else 1

    with open(stdout_path, "rb") as f:
        stdout_data = f.read()
    with open(stderr_path, "rb") as f:
        stderr_data = f.read()

    plan_file = None
    if plan_text:
        plan_file = os.path.abspath(os.path.join(out_dir, "plan.md"))
        with open(plan_file, "w", encoding="utf-8") as f:
            f.write(plan_text)
    response_file = None
    if response_text:
        response_file = os.path.abspath(os.path.join(out_dir, "response.md"))
        with open(response_file, "w", encoding="utf-8") as f:
            f.write(response_text)

    if notes:
        error = "；".join(([error] if error else []) + notes)

    # model_match 只表示模型是否实际验证，不随 status 变化：见到模型事件、
    # 全部与允许集合精确相等、provider 相符且没有不完整事件。
    # 中途被信号终止但已验证模型时可为 true，status 仍不满足通过门。
    model_verified = (
        observed_model is not None
        and incomplete_events == 0
        and all(model_matches(m) for m in models_seen)
        and (args.expect_provider is None
             or (bool(providers_seen)
                 and all(p == args.expect_provider for p in providers_seen)))
    )

    ended_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "label": args.label,
        "requested_model": args.requested_model,
        "expect_model": args.expect_model,
        "accepted_cursor_models": accepted_cursor_models,
        "observed_model": observed_model,
        "expect_provider": args.expect_provider,
        "observed_provider": observed_provider,
        "model_field": args.model_field,
        "provider_field": args.provider_field,
        "models_seen": models_seen,
        "providers_seen": providers_seen,
        "model_event_count": model_event_count,
        "provider_event_count": provider_event_count,
        "incomplete_events": incomplete_events,
        # 缺模型事件时视为未验证，不得记为匹配。
        "model_match": model_verified,
        "exit_code": exit_code,
        "child_exit_code": child_exit_code,
        "termination": termination,
        "signal": signal.Signals(signum).name if signum is not None else None,
        "status": status,
        "error": error,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(time.monotonic() - start_mono, 6),
        "stdout_bytes": len(stdout_data),
        "stdout_sha256": sha256_hex(stdout_data),
        "stderr_bytes": len(stderr_data),
        "stderr_sha256": sha256_hex(stderr_data),
        "json_lines": json_lines,
        "non_json_lines": non_json_lines,
        "usage": usage,
        "plan_file": plan_file,
        "response_file": response_file,
        "executable": child_argv[0],
        "arg_count": len(child_argv),
        "argv_sha256": sha256_hex(json.dumps(child_argv, ensure_ascii=False).encode("utf-8")),
    }
    compact = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(compact + "\n")
    try:
        print(compact, flush=True)
    except OSError:
        # 调用方已断开 stdout（例如终端关闭引发 SIGHUP）：summary 已落盘，退出码以它为准。
        # 把 stdout 指向 /dev/null，免得解释器退出时再次冲刷失败而改写退出码。
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
