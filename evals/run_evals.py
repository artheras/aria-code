#!/usr/bin/env python3
import sys
import os
import pathlib
import yaml
import asyncio
import inspect
from rich.console import Console
from rich.table import Table
from pydantic import BaseModel, Field

try:
    from google import genai
    from google.genai import types
    from google.auth import default as google_auth_default
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

# Add project root to sys.path
PROJECT_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import aria_cli
from apps.cli.context import AriaContext

class EvalResult(BaseModel):
    passed: bool = Field(description="Whether the output satisfies the rubric.")
    reason: str = Field(description="Reasoning for the evaluation.")

def llm_judge(prompt: str, output: str, rubric: str) -> EvalResult:
    if not HAS_GENAI:
        return EvalResult(passed=False, reason="google-genai not installed. Cannot use LLM judge.")
    
    try:
        try:
            _, project_id = google_auth_default()
        except Exception:
            project_id = None

        if project_id:
            client = genai.Client(vertexai=True, project=project_id, location="us-central1")
        elif os.environ.get("GEMINI_API_KEY"):
            client = genai.Client()
        else:
            return EvalResult(passed=False, reason="No Google Cloud ADC found and no GEMINI_API_KEY.")

        judge_prompt = f"""
You are an expert AI evaluator. 
I will provide a User Prompt, the Agent's Output, and an Evaluation Rubric.
Your job is to determine if the Agent's Output satisfies the Rubric.

User Prompt: {prompt}

Agent's Output:
{output}

Evaluation Rubric:
{rubric}
"""
        response = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=judge_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EvalResult,
                temperature=0.0
            ),
        )
        return response.parsed
    except Exception as e:
        return EvalResult(passed=False, reason=f"LLM Judge API Error: {str(e)}")

class MockConsole:
    def __init__(self):
        self.output = []
    
    def print(self, *args, **kwargs):
        text = " ".join(str(a) for a in args)
        self.output.append(text)
        
    def get_full_output(self):
        return "\n".join(self.output)
        
    def status(self, *args, **kwargs):
        class DummyCtx:
            def __enter__(self): return self
            def __exit__(self, *_): pass
        return DummyCtx()

async def run_evaluation(case):
    prompt = case["prompt"]
    expected_contains = case.get("expected_output_contains", [])
    eval_rubric = case.get("eval_rubric", None)
    
    mock_console = MockConsole()
    
    # Monkeypatch global console in aria_cli so all commands write to our buffer
    original_console = getattr(aria_cli, "console", None)
    aria_cli.console = mock_console

    try:
        # Initialize the real Terminal to use real data & models
        config = aria_cli.load_config()
        # Override model if needed, but we'll use user's default (probably Gemini or local)
        terminal = aria_cli.ArtheraTerminal(config)
        
        # Override console contexts
        terminal.context.console = mock_console
        
        commands = aria_cli.SlashCommands(terminal)
        commands.context = terminal.context
        
        # If terminal needs to send message, it uses rich print. 
        # Our mock console will catch it!
        
        if prompt.startswith("/"):
            parts = prompt.split(" ", 1)
            cmd = parts[0][1:]
            args = parts[1] if len(parts) > 1 else ""
            
            method_name = f"cmd_{cmd}"
            if hasattr(commands, method_name):
                method = getattr(commands, method_name)
                if inspect.iscoroutinefunction(method):
                    await method(args)
                else:
                    method(args)
            else:
                mock_console.print(f"Unknown command: {cmd}")
        else:
            await terminal.send_message(prompt)
            
    except Exception as e:
        mock_console.print(f"ERROR: {str(e)}")
    finally:
        if original_console:
            aria_cli.console = original_console
        
    # Evaluate Output
    full_output = mock_console.get_full_output()
    # terminal.conversation contains the LLM responses
    for msg in getattr(terminal, "conversation", []):
        if msg.get("role") == "assistant" and msg.get("content"):
            full_output += "\n" + msg["content"]
            
    passed = True
    fail_reasons = []
    
    for expected in expected_contains:
        if expected not in full_output:
            passed = False
            fail_reasons.append(f"Missing expected string: '{expected}'")
            
    if eval_rubric:
        judge_res = llm_judge(prompt, full_output, eval_rubric)
        if not judge_res.passed:
            passed = False
            fail_reasons.append(f"LLM Judge Failed: {judge_res.reason}")
            
    return passed, fail_reasons, full_output

async def main():
    config_path = pathlib.Path(__file__).parent / "datasets" / "basic_dev.yaml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
        
    cases = config.get("cases", [])
    
    console = Console()
    table = Table(title="Agentic Evaluation Harness Results (Real Data)")
    table.add_column("ID", style="cyan")
    table.add_column("Result", style="bold")
    table.add_column("Details", style="dim")
    
    total = len(cases)
    passed_count = 0
    
    for case in cases:
        passed, fail_reasons, _ = await run_evaluation(case)
        if passed:
            passed_count += 1
            table.add_row(case["id"], "[green]PASS[/green]", "Matched rubric/expects")
        else:
            table.add_row(case["id"], "[red]FAIL[/red]", "\n".join(fail_reasons))
            
    console.print(table)
    console.print(f"\n[bold]Total: {total} | Passed: {passed_count} | Failed: {total - passed_count}[/bold]")
    
if __name__ == "__main__":
    import logging
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    asyncio.run(main())
