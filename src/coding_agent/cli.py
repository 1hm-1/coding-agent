from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from coding_agent.application import AgentApplication
from coding_agent.domain import RunResult, RuntimeState
from coding_agent.evaluation import EvaluationReport, EvaluationRunner, load_eval_suite
from coding_agent.models.anthropic import AnthropicBackend
from coding_agent.models.base import ModelBackend
from coding_agent.models.openai_compatible import OpenAICompatibleBackend
from coding_agent.models.scripted import ScriptedBackend


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="Run, resume, inspect, and replay a persistent coding-agent session.",
    )
    parser.add_argument(
        "--agent-home",
        default=str(Path.home() / ".coding-agent"),
        help="Directory for isolated workspaces and trajectories.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-scripted", help="Run a scripted coding task.")
    run_parser.add_argument("--source", required=True, help="Read-only source repository path.")
    run_parser.add_argument("--task", required=True, help="Coding task for the agent.")
    run_parser.add_argument("--script", required=True, help="JSON scripted backend responses.")

    provider_parser = subparsers.add_parser("run", help="Run with a real model adapter.")
    provider_parser.add_argument("--provider", required=True, choices=("openai-compatible", "anthropic"))
    provider_parser.add_argument("--model", required=True)
    provider_parser.add_argument("--source", required=True)
    provider_parser.add_argument("--task", required=True)
    provider_parser.add_argument("--base-url")
    provider_parser.add_argument("--api-key-env")
    provider_parser.add_argument("--timeout", type=float, default=60.0)

    replay_parser = subparsers.add_parser("replay", help="Replay a saved trajectory.")
    replay_parser.add_argument("--session-id", required=True)

    subparsers.add_parser("sessions", help="List persisted sessions.")

    show_parser = subparsers.add_parser("show", help="Show a persisted session and call journal.")
    show_parser.add_argument("--session-id", required=True)

    resume_parser = subparsers.add_parser("resume", help="Resume a persisted session.")
    resume_parser.add_argument("--session-id", required=True)
    resume_parser.add_argument("--script", help="JSON scripted backend responses.")
    resume_parser.add_argument(
        "--provider", choices=("openai-compatible", "anthropic"),
        help="Use a real provider adapter instead of --script.",
    )
    resume_parser.add_argument("--model")
    resume_parser.add_argument("--base-url")
    resume_parser.add_argument("--api-key-env")
    resume_parser.add_argument("--timeout", type=float, default=60.0)

    interrupt_parser = subparsers.add_parser(
        "interrupt", help="Request a safe-boundary interruption."
    )
    interrupt_parser.add_argument("--session-id", required=True)

    resolve_parser = subparsers.add_parser(
        "resolve-call", help="Resolve an uncertain tool side effect."
    )
    resolve_parser.add_argument("--session-id", required=True)
    resolve_parser.add_argument("--call-id", required=True)
    resolve_parser.add_argument(
        "--resolution",
        required=True,
        choices=("effect-not-applied", "effect-applied", "abort"),
    )
    resolve_parser.add_argument("--actor", default="local-operator")
    resolve_parser.add_argument("--reason", default="operator_resolution")
    resolve_parser.add_argument(
        "--result-json",
        help="ToolResult JSON required for effect-applied resolution.",
    )

    export_parser = subparsers.add_parser(
        "export-trace", help="Rebuild a JSONL trace from committed SQLite events."
    )
    export_parser.add_argument("--session-id", required=True)
    export_parser.add_argument("--output", help="Optional JSONL destination path.")

    eval_parser = subparsers.add_parser("evaluate", help="Run a versioned evaluation suite.")
    eval_parser.add_argument("--suite", required=True, help="JSON eval suite manifest.")
    eval_parser.add_argument("--suite-root", help="Root for relative fixtures and backend scripts.")
    eval_parser.add_argument("--output", help="Evaluation output directory.")
    eval_parser.add_argument("--repetitions", type=int, default=1)
    eval_parser.add_argument(
        "--variant",
        choices=("passthrough", "budgeted", "compressed"),
        default="budgeted",
    )
    eval_parser.add_argument(
        "--ab",
        action="store_true",
        help="Run paired passthrough/budgeted variants.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "evaluate":
        suite, manifest_root = load_eval_suite(arguments.suite)
        runner = EvaluationRunner(
            arguments.agent_home,
            suite_root=arguments.suite_root or manifest_root,
        )
        if arguments.ab:
            result = runner.run_ab(
                suite,
                repetitions=arguments.repetitions,
                output_dir=arguments.output,
            )
            serializable = {
                key: (value.to_dict() if isinstance(value, EvaluationReport) else value)
                for key, value in result.items()
            }
        else:
            report = runner.run(
                suite,
                repetitions=arguments.repetitions,
                variant=arguments.variant,
                output_dir=arguments.output,
            )
            serializable = {
                **report.to_dict(),
                "output_dir": str(report.output_dir),
            }
        print(json.dumps(serializable, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    application = AgentApplication(arguments.agent_home)
    if arguments.command == "run-scripted":
        backend = ScriptedBackend.from_file(arguments.script)
        result = application.run_task(
            source=arguments.source,
            task=arguments.task,
            backend=backend,
        )
        print(json.dumps(_run_result_dict(result), ensure_ascii=False, indent=2))
        return 0 if result.state is RuntimeState.COMPLETED else 1

    if arguments.command == "run":
        backend = _provider_backend(
            arguments.provider,
            arguments.model,
            arguments.base_url,
            arguments.api_key_env,
            arguments.timeout,
        )
        result = application.run_task(
            source=arguments.source,
            task=arguments.task,
            backend=backend,
        )
        print(json.dumps(_run_result_dict(result), ensure_ascii=False, indent=2))
        return 0 if result.state is RuntimeState.COMPLETED else 1

    if arguments.command == "resume":
        if arguments.script and arguments.provider:
            raise ValueError("resume accepts either --script or --provider, not both")
        if arguments.script:
            backend = ScriptedBackend.from_file(arguments.script)
        elif arguments.provider and arguments.model:
            backend = _provider_backend(
                arguments.provider,
                arguments.model,
                arguments.base_url,
                arguments.api_key_env,
                arguments.timeout,
            )
        else:
            raise ValueError("resume requires --script or --provider with --model")
        result = application.resume_session(arguments.session_id, backend=backend)
        print(json.dumps(_run_result_dict(result), ensure_ascii=False, indent=2))
        return 0 if result.state is RuntimeState.COMPLETED else 1

    if arguments.command == "interrupt":
        requested_at = application.interrupt_session(arguments.session_id)
        print(
            json.dumps(
                {"session_id": arguments.session_id, "interrupt_requested_at": requested_at},
                ensure_ascii=False,
            )
        )
        return 0

    if arguments.command == "resolve-call":
        result = None
        if arguments.result_json:
            decoded = json.loads(arguments.result_json)
            if not isinstance(decoded, dict):
                raise ValueError("--result-json must contain an object")
            result = decoded
        snapshot = application.resolve_call(
            arguments.session_id,
            arguments.call_id,
            arguments.resolution,
            actor=arguments.actor,
            reason=arguments.reason,
            result=result,
        )
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if arguments.command == "sessions":
        print(json.dumps(application.list_sessions(), ensure_ascii=False, indent=2))
        return 0

    if arguments.command == "show":
        print(json.dumps(application.show_session(arguments.session_id), ensure_ascii=False, indent=2))
        return 0

    if arguments.command == "export-trace":
        path = application.export_trace(arguments.session_id, arguments.output)
        print(json.dumps({"session_id": arguments.session_id, "trace_path": str(path)}))
        return 0

    result = application.replay_session(arguments.session_id)
    print(
        json.dumps(
            {
                "session_id": result.session_id,
                "final_state": result.final_state.value,
                "event_count": result.event_count,
                "model_calls": result.model_calls,
                "tool_calls": result.tool_calls,
                "tool_failures": result.tool_failures,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "test_runs": result.test_runs,
                "final_test_passed": result.final_test_passed,
                "source_unchanged": result.source_unchanged,
                "failure_kind": result.failure_kind,
                "tool_status_counts": result.tool_status_counts,
                "tool_order": list(result.tool_order),
                "test_outcomes": list(result.test_outcomes),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_result_dict(result: RunResult) -> dict[str, object]:
    return {
        "session_id": result.session_id,
        "state": result.state.value,
        "final_answer": result.final_answer,
        "failure": result.failure,
        "workspace_path": result.workspace_path,
        "trace_path": result.trace_path,
        "step_count": result.step_count,
        "model_calls": result.model_calls,
        "tool_calls": result.tool_calls,
    }


def _provider_backend(
    provider: str,
    model: str,
    base_url: str | None,
    api_key_env: str | None,
    timeout: float,
) -> ModelBackend:
    if provider == "openai-compatible":
        return OpenAICompatibleBackend(
            model=model,
            base_url=base_url or "https://api.openai.com/v1",
            api_key_env=api_key_env or "OPENAI_API_KEY",
            timeout=timeout,
        )
    return AnthropicBackend(
        model=model,
        base_url=base_url or "https://api.anthropic.com/v1",
        api_key_env=api_key_env or "ANTHROPIC_API_KEY",
        timeout=timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
