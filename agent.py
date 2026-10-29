"""Optimized coding agent with directory discovery, code search, sandbox execution, and verification discipline."""

import os
import re
import subprocess
import sys
import warnings

from google.adk.agents import Agent
from google.genai import types

# The default model, shared with eval.py so there is a single source of truth.
DEFAULT_MODEL = "gemini-3.1-flash-lite"

# Silencing an ADK warning to declutter terminal output
warnings.filterwarnings("ignore", message=r".*JSON_SCHEMA_FOR_FUNC_DECL.*", category=UserWarning)


def _resolve(sandbox: str, path: str) -> str:
    """Turn a sandbox-relative path into an absolute one, refusing to escape the sandbox."""
    sandbox = os.path.realpath(sandbox)
    full = os.path.realpath(os.path.join(sandbox, path))
    # Allow only the sandbox root itself or a path beneath it (blocks "../" escapes).
    if full != sandbox and not full.startswith(sandbox + os.sep):
        raise ValueError(f"path escapes sandbox: {path!r}")
    return full


# The helpers below return errors as plain strings instead of raising, so the
# model sees what went wrong and can recover on its next turn.

def _read(sandbox: str, path: str) -> str:
    try:
        with open(_resolve(sandbox, path), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return f"Error: no such file: {path}"
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


def _write(sandbox: str, path: str, contents: str) -> str:
    try:
        full = _resolve(sandbox, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(contents)
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"
    return f"Wrote {len(contents)} chars to {path}"


def _list_dir(sandbox: str, path: str = ".") -> str:
    try:
        target = _resolve(sandbox, path)
        if not os.path.exists(target):
            return f"Error: path does not exist: {path}"
        if os.path.isfile(target):
            return os.path.relpath(target, sandbox)
        entries = []
        for root, dirs, files in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
            for f in sorted(files):
                if f.endswith(".pyc"):
                    continue
                rel = os.path.relpath(os.path.join(root, f), sandbox)
                entries.append(rel)
        return "\n".join(entries) if entries else "(empty directory)"
    except (ValueError, OSError) as exc:
        return f"Error: {exc}"


def _search_files(sandbox: str, pattern: str, path: str = ".") -> str:
    try:
        target = _resolve(sandbox, path)
        regex = re.compile(pattern)
        matches = []
        for root, dirs, files in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
            for f in sorted(files):
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, sandbox)
                try:
                    with open(full, encoding="utf-8") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if regex.search(line):
                                matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                except OSError:
                    continue
        return "\n".join(matches) if matches else "No matches found."
    except (ValueError, re.error, OSError) as exc:
        return f"Error: {exc}"


def _run_command(sandbox: str, command: str, stdin_input: str = "") -> dict:
    try:
        sandbox_real = os.path.realpath(sandbox)
        env = os.environ.copy()
        venv_bin = os.path.dirname(sys.executable)
        env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
        env["PYTHONPATH"] = f"{sandbox_real}:{os.path.join(sandbox_real, 'src')}"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            command,
            shell=True,
            cwd=sandbox_real,
            input=stdin_input,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": "Error: Command timed out after 15s"}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": f"Error: {exc}"}


SYSTEM_INSTRUCTION = """You are an autonomous coding engineer. Solve the user's task by inspecting the workspace, writing clean Python code, and rigorously verifying your solution before finishing.

### 1. Root-Cause Investigation (Bug-Fix Tasks)
- **Discover the full package first**: Always start by calling `list_dir` (and `search_files` if helpful) to see every file in `src/`.
- **Do not stop at the file named in the bug report**: Bug reports often describe where the wrong value surfaces (the symptom), while the actual defect lives upstream in a sibling parsing, loading, or normalization module. Read all modules in the package (including `__init__.py` and upstream helpers) to trace the entire data flow from raw input to final output before editing.
- **Reproduce the bug first**: Use `run_command` to run a quick Python check exercising the full pipeline (from upstream parse/load functions to the downstream reporting function) to observe the failure firsthand.
- **Fix the root cause, not the symptom (Minimal surgical edit)**: Repair ONLY the upstream defect where data is truncated or stripped (e.g., changing `int(float(raw))` to `float(raw)`, or removing `.replace(tzinfo=None)` on parsed ISO-8601 datetimes). If the downstream function named in the prompt is already correct, DO NOT edit it! In bug-fix tasks that use Python `float`, NEVER replace `float` with `Decimal` (because `Decimal("125.49") == 125.49` is `False` in Python assertions).

### 2. Edge-Case & Output-Contract Rigor (CLI Build-from-Scratch Tasks ONLY)
- **Always write code using `write_file`**: Never just print the solution in your response. Write the complete implementation to the target file path in `src/`.
- **Systematically enumerate and handle boundary conditions**:
  1. **Ordering & sequence direction**: Never assume ranges or sequences are only ascending. If given a range `a-b` where `a > b` (a descending range such as `9-7`), expand it in descending order (`9 8 7`, stepping by `-1`) from `a` to `b` inclusive.
  2. **Exact decimal math & half-up money rounding**: Use `decimal.Decimal` with `ROUND_HALF_UP` (`d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)`) for currency and fixed-precision rounding (so `5.005` rounds to `5.01`). When summing `Decimal` values, always initialize `sum(..., Decimal("0"))` so an empty input produces `Decimal("0")` rather than integer `0` (avoiding `AttributeError: 'int' object has no attribute 'quantize'`).
  3. **Trailing-zero trimming**: When asked to print plain decimals with trailing zeros trimmed (e.g., `4.0` -> `4`, `1.50` -> `1.5`), format with `f"{d:f}"` and only strip trailing zeros if a decimal point is present (`s.rstrip("0").rstrip(".") if "." in s else s`), avoiding `-0` (`"0"` if zero).
  4. **Empty & blank inputs**: Explicitly handle empty stdin (`""` or only blank lines) for every subcommand (`total`, `average`, `length`, `merge`, `count`, etc.) without crashing or dividing by zero.
  5. **Exit codes & stderr**: Validate CLI arguments, subcommands, and malformed input lines strictly. On usage or input errors, print an error message to `sys.stderr` (nothing to `stdout`) and exit with the exact specified exit code (e.g., `sys.exit(2)` for usage/malformed input, `sys.exit(1)` for missing category).

### 3. Mandatory Verification Loop
- Never finish without executing your code via `run_command`.
- For bug fixes: run a Python test verifying the full end-to-end flow (upstream parser + downstream function) on the prompt's case and additional offsets/inputs.
- For CLI programs: invoke the script via `run_command` with `stdin_input` covering:
  - All examples from the prompt.
  - Empty input (`stdin_input=""`) across subcommands (`total`, `average`, `length`, `count`, `merge`).
  - Reversed/descending ranges (e.g. `9-7`), single elements, and touching intervals.
  - Malformed input lines, unknown commands, and missing arguments (checking both `returncode` and non-empty `stderr`).
- If any test fails, diagnose the output, update the file with `write_file`, and re-run `run_command` until every check passes.
"""


def build_agent(sandbox: str, model: str) -> Agent:
    # Each tool's docstring is the description the model sees, so it states that
    # paths are sandbox-relative.
    def list_dir(path: str = ".") -> dict:
        """List all files recursively under a directory path (relative to the project root)."""
        return {"result": _list_dir(sandbox, path)}

    def search_files(pattern: str, path: str = ".") -> dict:
        """Search for a regex pattern across files under path (relative to the project root)."""
        return {"result": _search_files(sandbox, pattern, path)}

    def read_file(path: str) -> dict:
        """Read a text file (path is relative to the project root) and return its contents."""
        return {"result": _read(sandbox, path)}

    def write_file(path: str, contents: str) -> dict:
        """Create or overwrite a text file (path is relative to the project root)."""
        return {"result": _write(sandbox, path, contents)}

    def run_command(command: str, stdin_input: str = "") -> dict:
        """Run a shell/python command inside the sandbox root, optionally passing stdin_input, and return returncode, stdout, and stderr."""
        return {"result": _run_command(sandbox, command, stdin_input)}

    return Agent(
        name="coding_agent",
        model=model,
        instruction=SYSTEM_INSTRUCTION,
        tools=[list_dir, search_files, read_file, write_file, run_command],
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2,
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.LOW
            )
        )
    )


if __name__ == "__main__":
    # Test the agent on a prompt in ./out sandbox.  Usage: python agent.py "your prompt"
    import asyncio
    import glob
    from google.adk.runners import InMemoryRunner

    # Default to Vertex AI (like eval.py) so `python agent.py` works without a
    # Gemini API key; an existing env value, if set, takes precedence.
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "qwiklabs-gcp-01-78c60f131565")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

    sandbox = os.path.join(os.path.dirname(os.path.realpath(__file__)), "out")
    os.makedirs(os.path.join(sandbox, "src"), exist_ok=True)
    prompt = " ".join(sys.argv[1:]) or "Write a hello world python script"

    async def _run_agent() -> None:
        runner = InMemoryRunner(agent=build_agent(sandbox, DEFAULT_MODEL))
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id="me")
        message = types.Content(role="user", parts=[types.Part(text=prompt)])
        async for event in runner.run_async(user_id="me", session_id=session.id, new_message=message):
            for part in (event.content and event.content.parts) or []:
                print(part.text or "", end="", flush=True)
    asyncio.run(_run_agent())
    for script in glob.glob(f"{sandbox}/**/*.py", recursive=True):
        print(f"\n=== output of {os.path.relpath(script, sandbox)} ===\n" + subprocess.run([sys.executable, script], cwd=sandbox, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout, end="")
