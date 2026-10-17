#!/usr/bin/env python3
"""work-closure 唯一 CLI 包装器。

包装 cursor-agent 或 ZCode 的子进程调用：把 stdout 与 stderr 原样落盘，
逐行解析 stream-json 事件确认实际 provider/model，另存真实计划与响应文本，
最后只向本进程 stdout 打印一条紧凑 JSON summary。仅依赖 Python 标准库。

summary 不记录完整 command、prompt、环境或用户任务文本，只记录 executable、
参数数量与参数数组的 SHA256，长输入也保持紧凑。
"""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

_MISSING = object()


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


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
            # 独立进程组启动：终止时只覆盖本包装器拥有的进程组，
            # 不影响外层 PM2 父进程或其他任务。
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
            def drain_stderr():
                try:
                    while True:
                        chunk = proc.stderr.read(65536)
                        if not chunk:
                            break
                        err_f.write(chunk)
                finally:
                    try:
                        proc.stderr.close()
                    except OSError:
                        pass

            def terminate_group():
                """只终止本包装器启动的进程组（含被包装 CLI 的后代）。"""
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    try:
                        proc.kill()
                    except OSError:
                        pass

            err_thread = threading.Thread(target=drain_stderr, daemon=True)
            err_thread.start()
            try:
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
                            # 实际模型事件明确不符：立即终止本包装器拥有的
                            # 进程组，停止后续工具副作用，不再消费剩余输出。
                            status = "model_mismatch"
                            termination = failure
                            exit_code = 3
                            terminate_group()
                            break
                # 无论自然结束还是主动终止，都实际收割子进程并记录真实退出码。
                try:
                    proc.stdout.close()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    terminate_group()
                    proc.wait(timeout=10)
                child_exit_code = proc.returncode
            finally:
                if proc.poll() is None:
                    terminate_group()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                if err_thread is not None and err_thread.is_alive():
                    err_thread.join(timeout=10)
    finally:
        out_f.close()
        err_f.close()

    if status is None:
        if observed_model is None:
            # 没有任何模型事件：模型未验证。子进程真实非零退出码优先保留，
            # 不被 no_model_event 覆盖；仅当子进程本身正常退出时才按
            # 模型校验失败返回 3。缺模型经 termination 与 model_match 标明。
            termination = "no_model_event"
            if child_exit_code is not None and child_exit_code != 0:
                status = "child_failed"
                exit_code = child_exit_code
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
                exit_code = child_exit_code
                status = "ok" if child_exit_code == 0 else "child_failed"

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
        "model_match": observed_model is not None and status != "model_mismatch",
        "exit_code": exit_code,
        "child_exit_code": child_exit_code,
        "termination": termination,
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
    print(compact)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
