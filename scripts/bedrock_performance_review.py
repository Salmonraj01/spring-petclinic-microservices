#!/usr/bin/env python3
"""Local-first Java/.NET performance reviewer powered by AWS Bedrock Claude Haiku 4.5.

Credentials are read from the standard AWS environment/profile chain. This tool
never reads credentials from source files or prints them.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import boto3

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = ROOT / "performance-review-rules"
MAX_FINDINGS = 5
IGNORED_PARTS = ("/src/test/", "/test/", "/tests/", "/generated/", "/migrations/")


def load_rules():
    cm_path = ROOT / ".cm" / "gitstream.cm"
    combined_path = RULES_DIR / "COMBINED_PERF_RULES.md"
    java_path = RULES_DIR / "JAVA_PERF_RULES.md"
    dotnet_path = RULES_DIR / "DOTNET_PERF_RULES.md"
    review_path = RULES_DIR / "REVIEW_RULES.md"
    required = (cm_path, combined_path, java_path, dotnet_path, review_path)
    missing = [str(p.relative_to(ROOT)) for p in required if not p.is_file()]
    if missing:
        raise FileNotFoundError("Missing review policy files: " + ", ".join(missing))

    cm = cm_path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s*issues_limit:\s*(\d+)\s*$", cm)
    limit = min(MAX_FINDINGS, int(match.group(1))) if match else MAX_FINDINGS
    return {
        "gitstream_config": cm,
        "combined": combined_path.read_text(encoding="utf-8"),
        "java": java_path.read_text(encoding="utf-8"),
        "dotnet": dotnet_path.read_text(encoding="utf-8"),
        "review_rules": review_path.read_text(encoding="utf-8"),
        "limit": limit,
    }


def get_diff(args):
    if args.diff_file:
        return Path(args.diff_file).read_text(encoding="utf-8")
    command = ["git", "diff", "--no-ext-diff", "--unified=8"]
    if args.base_ref:
        command.append(f"{args.base_ref}...HEAD")
    else:
        command.append("HEAD")
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return result.stdout


def parse_diff(diff_text, rules):
    chunks = re.split(r"(?m)(?=^diff --git )", diff_text)
    files = []
    for chunk in chunks:
        if not chunk.startswith("diff --git "):
            continue
        match = re.search(r"(?m)^\+\+\+ b/(.+)$", chunk)
        if not match:
            continue
        path = match.group(1).strip()
        lower = "/" + path.lower()
        if path.endswith(".java"):
            language = "java"
            language_rules = rules["java"] + "\n\n" + rules["review_rules"]
        elif path.endswith(".cs"):
            language = "dotnet"
            language_rules = rules["dotnet"]
        else:
            continue
        if any(part in lower for part in IGNORED_PARTS):
            continue
        if path.lower().endswith((".designer.cs", ".g.cs", ".generated.cs")):
            continue
        files.append({
            "path": path,
            "language": language,
            "patch": chunk[:24000],
            "language_rules": language_rules,
        })
    return files


def call_claude(prompt, region):
    # boto3 resolves AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and
    # AWS_SESSION_TOKEN automatically, or uses the configured AWS profile.
    client = boto3.client("bedrock-runtime", region_name=region)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4000,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = client.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    payload = json.loads(response["body"].read())
    return payload["content"][0]["text"]


def parse_model_json(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Claude response did not contain a JSON object")
    return json.loads(cleaned[start:end + 1])


def main():
    parser = argparse.ArgumentParser(description="Review local Java/.NET changes with AWS Bedrock Claude Haiku 4.5.")
    parser.add_argument("--base-ref", help="Git ref to compare against, e.g. origin/main. Default: HEAD (working tree changes).")
    parser.add_argument("--diff-file", help="Path to a unified diff file instead of running git diff.")
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--output", help="Optional JSON output file.")
    args = parser.parse_args()

    rules = load_rules()
    diff_text = get_diff(args)
    if not diff_text.strip():
        print("No Git diff found. Use --base-ref origin/main or --diff-file <patch>.")
        return 0

    files = parse_diff(diff_text, rules)
    if not files:
        print("No eligible Java or C# source changes found in the diff.")
        return 0

    prompt = f"""You are a strict L0 performance-only code reviewer.
Use the repository policy below as authoritative. Do not invent additional rules.
The .cm file is the existing gitStream configuration. The combined Markdown file
defines shared scope and language separation. Apply JAVA_PERF_RULES.md and
REVIEW_RULES.md only to Java; apply DOTNET_PERF_RULES.md only to C#.
Review only added/changed lines in the supplied unified diffs. Do not flag style,
correctness unrelated to performance, tests, generated code, migrations or build files.
Return ONLY JSON in this exact shape:
{{"findings":[{{"rule_id":"PERF-01","severity":"HIGH","file":"path","line":12,
"title":"short title","body":"Performance impact and concise fix; corrected snippet max 8 lines."}}]}}
Maximum {rules["limit"]} findings, highest severity first. Use exact rule IDs from
the policy. The line must be an added/changed line in the diff. If none, return
{{"findings":[]}}.
POLICY FILES:
{json.dumps({k: v for k, v in rules.items() if k != "limit"}, ensure_ascii=False)}
CHANGED SOURCE DIFFS:
{json.dumps(files, ensure_ascii=False)}
"""
    raw = call_claude(prompt, args.region)
    result = parse_model_json(raw)
    findings = result.get("findings", [])
    allowed = {item["path"] for item in files}
    findings = [
        item for item in findings
        if item.get("file") in allowed
        and isinstance(item.get("line"), int)
        and item.get("line") > 0
        and item.get("severity") in {"HIGH", "MEDIUM", "LOW"}
        and isinstance(item.get("rule_id"), str)
        and isinstance(item.get("body"), str)
    ][:rules["limit"]]

    output = {
        "model": MODEL_ID,
        "region": args.region,
        "files_reviewed": [item["path"] for item in files],
        "findings": findings,
        "finding_count": len(findings),
    }
    rendered = json.dumps(output, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        print(f"Review saved to {args.output}")
    else:
        print(rendered)
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
