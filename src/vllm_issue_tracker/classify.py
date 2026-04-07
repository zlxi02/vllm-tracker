from __future__ import annotations

import re


PATTERN_GROUPS: dict[str, list[tuple[str, str]]] = {
    "model": [
        (r"\bqwen(?:2(?:\.5)?|3)?(?:[-\w.]*)\b", "Qwen"),
        (r"\bdeepseek(?:[-\w.]*)\b", "DeepSeek"),
        (r"\bllama(?:[-\w.]*)\b", "Llama"),
        (r"\bgemma(?:[-\w.]*)\b", "Gemma"),
        (r"\bmistral(?:[-\w.]*)\b", "Mistral"),
        (r"\bmixtral(?:[-\w.]*)\b", "Mixtral"),
        (r"\bphi(?:[-\w.]*)\b", "Phi"),
        (r"\byi(?:[-\w.]*)\b", "Yi"),
        (r"\b(?:chat)?glm(?:[-\w.]*)\b", "GLM"),
        (r"\bminimax(?:[-\w.]*)\b", "MiniMax"),
        (r"\bfalcon(?:[-\w.]*)\b", "Falcon"),
        (r"\binternlm(?:[-\w.]*)\b", "InternLM"),
        (r"\bbaichuan(?:[-\w.]*)\b", "Baichuan"),
        (r"\bstarcoder(?:[-\w.]*)\b", "StarCoder"),
        (r"\bcommand[-\s]?r(?:[-\w.]*)\b", "Command-R"),
        (r"\bjamba(?:[-\w.]*)\b", "Jamba"),
        (r"\bmamba(?:[-\w.]*)\b", "Mamba"),
        (r"\bgpt(?:[-\w.]*)\b", "GPT"),
        (r"\bnemotron(?:[-\w.]*)\b", "Nemotron"),
        (r"\bgranite(?:[-\w.]*)\b", "Granite"),
        (r"\bcohere(?:[-\w.]*)\b", "Cohere"),
    ],
    "hardware": [
        (r"\bgb200\b", "GB200"),
        (r"\bb200\b", "B200"),
        (r"\bh200\b", "H200"),
        (r"\bh100\b", "H100"),
        (r"\ba100\b", "A100"),
        (r"\bl40s?\b", "L40S"),
        (r"\bmi300x?\b", "MI300"),
        (r"\bmi355x?\b", "MI355"),
        (r"\brocm\b", "ROCm"),
        (r"\bamd\b", "AMD"),
        (r"\bcuda\b", "CUDA"),
        (r"\bjetson\b", "Jetson"),
        (r"\bcpu\b", "CPU"),
        (r"\btpu\b", "TPU"),
    ],
}


FAILURE_MODE_PATTERNS: list[tuple[str, tuple[str, str]]] = [
    (r"\boom\b|\bout of memory\b|\bkv cache\b|\bcpu ram\b|\bram growth\b", ("oom_memory_kv_cache", "OOM / memory / KV cache")),
    (r"\bhang\b|\bstuck\b|\bdeadlock\b|\bdisaggregation\b|\bmulti[- ]node\b|\bdistributed\b|\bep\b", ("distributed_multi_node", "Distributed / multi-node")),
    (r"\bcompile\b|\bkernel\b|\btriton\b|\bcudagraph\b|\bflashinfer\b|\bflash attention\b|\bflash-attn\b", ("compile_kernel_backend", "Compile / kernel / backend")),
    (r"\bslow\b|\bslower\b|\blatency\b|\bthroughput\b|\bperformance\b|\bacceptance rate\b", ("performance_regression", "Performance regression")),
    (r"\bwrong\b|\bincorrect\b|\baccuracy\b|\bgarbled\b|\bexclamation marks\b|\b!!!!!", ("incorrect_output", "Incorrect output")),
    (r"\bapi\b|\bopenai\b|\bresponses\b|\btool\b|\bfunction call\b|\bchat completion\b|\bprotocol\b", ("api_protocol_tool_calling", "API / tool-calling / protocol")),
    (r"\binstall(?:ation)?\b|\bbuild\b|\bundefined symbol\b|\bimporterror\b|\bmodule not found\b", ("install_environment", "Install / environment")),
    (r"\bunsupported\b|\bnot support(?:ed)?\b|\bmodel support\b|\btrust_remote_code\b", ("model_support_gap", "Model support gap")),
    (r"\bdoc(?:s|umentation)?\b|\bunclear\b|\busability\b|\bexample\b", ("docs_usability", "Docs / usability")),
    (r"\bcrash\b|\berror\b|\bexception\b|\btraceback\b|\bsegfault\b|\bfailed\b", ("crash_hard_failure", "Crash / hard failure")),
]


# NOTE: ROADMAP_PATTERNS are used for the legacy regex-based report pipeline.
# They are NOT used for dashboard classification. Dashboard SIG groups are
# assigned via LLM in the `dashboard-classify` command, using WORKSTREAM_THEMES
# from prompts.py as the canonical SIG list.
ROADMAP_PATTERNS: list[tuple[str, str]] = [
    (r"\bdisaggregation\b|\belastic ep\b|\bmulti[- ]node\b|\bdistributed\b|\brouter\b", "large_scale_serving"),
    (r"\bcompile\b|\btorch\.compile\b", "torch_compile"),
    (r"\bthroughput\b|\blatency\b|\bperformance\b|\bspeculative decoding\b|\bacceptance rate\b", "performance"),
    (r"\bmultimodal\b|\bvision\b|\bqwen\d?-vl\b|\bimage input\b|\bmulti-image\b|\baudio\b", "multimodality"),
    (r"\btool\b|\bfunction call\b|\bopenai\b|\bresponses\b|\bchat completion\b", "frontend_api"),
    (r"\bquant(?:ization)?\b|\bmxfp4\b|\bfp8\b|\bawq\b|\bgptq\b|\bspeculative decoding\b", "model_acceleration"),
    (r"\brocm\b|\bamd\b|\bcuda\b|\bkernel\b|\bflashinfer\b|\bflash attention\b", "core_engine"),
    (r"\bdocs\b|\bdocumentation\b|\bexample\b", "docs_ux"),
    (r"\bci\b|\brelease\b|\bbuild\b", "ci_release"),
    (r"\bgrpo\b|\brl\b|\breinforcement\b", "rl"),
    (r"\bunsupported\b|\bmodel support\b|\btrust_remote_code\b", "model_support"),
]


def normalize_text(*parts: str | None) -> str:
    return " ".join(part or "" for part in parts).lower()


def extract_tag(text: str, pattern_group: str) -> str:
    for pattern, tag in PATTERN_GROUPS[pattern_group]:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return tag
    return "Other"


def classify_failure_mode(text: str) -> tuple[str, str]:
    for pattern, result in FAILURE_MODE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return result
    return ("other", "Other")


def classify_roadmap_tag(text: str) -> str:
    for pattern, tag in ROADMAP_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return tag
    return "other"


def classify_failure_mode_with_fallback(primary_text: str, fallback_text: str) -> tuple[str, str]:
    primary = classify_failure_mode(primary_text)
    if primary[0] != "other":
        return primary
    return classify_failure_mode(fallback_text)


def classify_roadmap_tag_with_fallback(primary_text: str, fallback_text: str) -> str:
    primary = classify_roadmap_tag(primary_text)
    if primary != "other":
        return primary
    return classify_roadmap_tag(fallback_text)


def summarize_issue(title: str, failure_mode_label: str, model_tag: str, hardware_tag: str) -> str:
    parts = [failure_mode_label]
    if model_tag != "Other":
        parts.append(model_tag)
    if hardware_tag != "Other":
        parts.append(hardware_tag)
    parts.append(title.strip())
    return " | ".join(part for part in parts if part)
