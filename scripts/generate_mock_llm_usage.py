import json
from pathlib import Path
from typing import Any, cast

FIXTURE_DIR = Path("src/obs_platform/telemetry/v1/fixtures")
INPUT_RATE_USD = 0.000003
OUTPUT_RATE_USD = 0.000015
LATENCY_BASE_MS = 300
LATENCY_PER_COMPLETION_TOKEN_MS = 8


def estimate_tokens(payload: dict[str, Any] | None) -> int:
    if payload is None:
        return 0

    snippet = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return max(1, round(len(snippet) / 4))


def update_llm_call(llm_call: dict[str, Any]) -> None:
    prompt_tokens = estimate_tokens(
        cast(dict[str, Any] | None, llm_call["input_payload"])
    )
    completion_tokens = estimate_tokens(
        cast(dict[str, Any] | None, llm_call["output_payload"])
    )

    llm_call["prompt_tokens"] = prompt_tokens
    llm_call["completion_tokens"] = completion_tokens
    llm_call["total_tokens"] = prompt_tokens + completion_tokens
    llm_call["latency_ms"] = LATENCY_BASE_MS + (
        completion_tokens * LATENCY_PER_COMPLETION_TOKEN_MS
    )
    llm_call["estimated_cost_usd"] = round(
        prompt_tokens * INPUT_RATE_USD + completion_tokens * OUTPUT_RATE_USD,
        6,
    )


def update_fixture(path: Path) -> None:
    payload = cast(dict[str, Any], json.loads(path.read_text()))

    for llm_call in payload["llm_calls"]:
        update_llm_call(cast(dict[str, Any], llm_call))

    usage = cast(dict[str, Any], payload["usage"])
    usage["total_tokens"] = sum(
        cast(int, llm_call["total_tokens"]) for llm_call in payload["llm_calls"]
    )
    usage["total_estimated_cost_usd"] = round(
        sum(
            cast(float, llm_call["estimated_cost_usd"])
            for llm_call in payload["llm_calls"]
        ),
        6,
    )

    path.write_text(json.dumps(payload, indent=2) + "\n")


def main() -> None:
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        update_fixture(path)


if __name__ == "__main__":
    main()
