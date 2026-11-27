"""The installed headless loop with only the admitted BIM MCP provider."""
import json

DISABLED = [
    'tool-plugin-manager', 'tool-working-directory', 'tool-bash', 'tool-pwsh',
    'tool-jobs', 'tool-fs', 'tool-fs-search', 'agent-instructions', 'skill-filesystem',
    'tool-skill', 'plan-mode', 'goal-round-driver', 'tool-subagent-control',
    'tool-subagent-list-agents', 'tool-subagent', 'tool-subagent-fork', 'tool-workflow',
    'tool-todo', 'tool-goal', 'tool-web', 'mcp-resources', 'session-title-llm',
    'compaction-basic', 'llm-deepseek', 'llm-deepseek-account', 'session-telemetry-otel',
]
def make_patch(base_url, model, image_directory=None, *, worker_args=None, context_window=1050000,
               worker_wall_seconds=None, display_name="GPT Sol subscription relay", max_tokens=128000):
    # Keep classic profiles byte-identical, while quoting arbitrary catalog IDs as YAML scalars.
    import re
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", model) or display_name == "Custom model API":
        model = json.dumps(model, ensure_ascii=False)
    profile = f"""- id: agent-default-model
  config:
    provider: siwc-relay
    model: {model}
    reasoningEffort: high
- id: llm-pi-ai
  config:
    providers:
      siwc-relay:
        displayName: {display_name}
        apiKeyEnv: SIM_BIM_RELAY_TOKEN
        api: openai-responses
        baseURL: {json.dumps(base_url)}
        reasoning: high
        timeoutMs: 1530000
        streamIdleTimeoutMs: 1500000
        models:
          - id: {model}
            name: {model}
            contextWindow: {context_window}
            maxTokens: {max_tokens}
            input: [text, image]
            reasoningEfforts:
              high: high
        retryPolicy:
          mode: normal
          maxRetries: 0
""" + ''.join(f'- id: {identifier}\n  disabled: true\n' for identifier in DISABLED)
    if image_directory is not None:
        profile += f"""- insert:
    - id: sim-bim-context-images
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: sim_bim_context
        transport: stdio
        command: !!js process.env.SIM_BIM_REPO + '/.venv/Scripts/python.exe'
        cwd: !!js process.env.SIM_BIM_REPO
        args: ['-B', '-X', 'utf8', '-m', 'src.dsh_runtime.image_history', '--directory', {json.dumps(str(image_directory))}]
        failOnStartupError: true
"""
    if worker_args is not None:
        args = ['-B', '-X', 'utf8', '-m', 'src.agent.observer_mcp', *worker_args]
        # DSH defaults MCP calls to 60 seconds. A delegated observation can span
        # several model turns; the task and case already enforce their deadlines.
        max_timer_ms = 2147483647
        timeout_ms = (max_timer_ms if worker_wall_seconds is None else
                      min(max_timer_ms, max(1, int(worker_wall_seconds * 1000))))
        profile += f"""- insert:
    - id: sim-bim-observers
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: sim_bim_workers
        transport: stdio
        toolCallTimeoutMs: {timeout_ms}
        command: !!js process.env.SIM_BIM_REPO + '/.venv/Scripts/python.exe'
        cwd: !!js process.env.SIM_BIM_REPO
        args: {json.dumps(args)}
        failOnStartupError: true
"""
    return profile
