"""P15: prompt-only Codex components must have no file/command/browser tool access."""
from app.llm.codex_cli import CodexCLIProvider


def test_prompt_only_codex_disables_file_command_browser_and_subagent_tools():
    cmd = CodexCLIProvider()._cmd("gpt-6-luna", "low", "/scratch/schema.json", "/scratch/work")
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    disabled = {cmd[i + 1] for i, v in enumerate(cmd[:-1]) if v == "--disable"}
    assert {"shell_tool", "unified_exec", "view_image", "apps", "plugins", "remote_plugin", "hooks", "multi_agent",
            "multi_agent_v2", "browser_use", "browser_use_external", "computer_use", "code_mode_host", "image_generation",
            "skill_search", "skill_mcp_dependency_install"} <= disabled
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "read-only"
