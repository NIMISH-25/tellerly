"""LLM discovery: planner loop, recorder, and the trace->capability compiler.
The only package permitted to import the model SDK (google-genai; default
planner model: gemini-2.5-flash, see tellerly.config)."""
from tellerly.discovery.compiler import CompileError, compile_capability, load_outcome_catalog
from tellerly.discovery.engine import DiscoveryEngine
from tellerly.discovery.job import JobInput, JobSpec
from tellerly.discovery.planner import GeminiPlanner, Planner, ScriptedPlanner, ToolCall
from tellerly.discovery.recorder import RecorderError, build_candidates, measure_target

__all__ = [
    "CompileError",
    "DiscoveryEngine",
    "GeminiPlanner",
    "JobInput",
    "JobSpec",
    "Planner",
    "RecorderError",
    "ScriptedPlanner",
    "ToolCall",
    "build_candidates",
    "compile_capability",
    "load_outcome_catalog",
    "measure_target",
]
