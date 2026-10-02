"""Optional framework adapters for nabit.

Each adapter lives in its own module and lazily imports its framework, so the
core `nabit` package stays dependency-free. Import the one you need:

    from nabit.adapters.langgraph import NabitCallback, verify_node

Nothing here is imported by `nabit/__init__.py` — installing nabit never pulls
in LangGraph/LangChain.
"""
