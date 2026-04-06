"""Semantic codebase search for CrabCode."""

__all__ = ["CodebaseSearchTool"]


def __getattr__(name: str):
    if name == "CodebaseSearchTool":
        from crabcode_search.tool import CodebaseSearchTool

        return CodebaseSearchTool
    raise AttributeError(name)
