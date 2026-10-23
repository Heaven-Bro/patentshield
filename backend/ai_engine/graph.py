"""LangGraph workflow wiring for PatentShield claim analysis."""

from __future__ import annotations

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ai_engine.nodes import (
    deconstruct_claim_node,
    groundedness_guardrail_node,
    hybrid_retrieval_node,
    limitation_evaluator_node,
    scoring_node,
)
from ai_engine.state import AnalysisState


def build_patent_analysis_graph() -> StateGraph:
    """Build the sequential deconstruction, retrieval, and scoring graph."""
    graph = StateGraph(AnalysisState)
    graph.add_node("deconstruct_claim", deconstruct_claim_node)
    graph.add_node("hybrid_retrieval", hybrid_retrieval_node)
    graph.add_node("limitation_evaluator", limitation_evaluator_node)
    graph.add_node("groundedness_guardrail", groundedness_guardrail_node)
    graph.add_node("scoring", scoring_node)

    graph.set_entry_point("deconstruct_claim")
    graph.add_edge("deconstruct_claim", "hybrid_retrieval")
    graph.add_edge("hybrid_retrieval", "limitation_evaluator")
    graph.add_edge("limitation_evaluator", "groundedness_guardrail")
    graph.add_edge("groundedness_guardrail", "scoring")
    graph.add_edge("scoring", END)
    return graph


def compile_analysis_graph() -> CompiledStateGraph:
    """Compile and return the runnable claim-analysis workflow."""
    return build_patent_analysis_graph().compile()
