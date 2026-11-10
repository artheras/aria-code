# Aria + Nebius Token Factory: setup and verification

Aria Code already supports user-declared OpenAI-compatible providers. This is a configuration-based integration, not a claim that a live Nebius call or agent workflow has been verified. Model availability, tool calling and costs depend on the account and selected model.

1. In Nebius Token Factory, create an API key and identify an available NVIDIA model ID from your account's model catalogue. Never commit a key or paste one into chat.
2. In your shell, set `NEBIUS_API_KEY` and `NEBIUS_MODEL` securely. Use a model ID beginning `nvidia/` from the actual catalogue, not an assumed name.
3. Run `python3 nebius_smoke.py`. It performs a real, potentially billable inference call. The output reports the requested/returned model and whether text was received; it does not print the key or model text. A successful completion is an API smoke test, not verification of Aria tool calling or contest compliance.
4. Add the following to your existing **user-level** `~/.aria/providers.yaml` or equivalent; merge with existing configuration and respect the documented configuration precedence. Do not overwrite an existing file:

```yaml
llm:
  default: nebius/nvidia/<model-id-from-catalogue>
model_providers:
  nebius:
    name: Nebius Token Factory
    base_url: https://api.tokenfactory.nebius.com/v1
    env_key: NEBIUS_API_KEY
    model: nvidia/<model-id-from-catalogue>
```

Replace the entire model placeholder with the exact ID returned by Nebius; `nebius/` is the Aria provider prefix and the model ID follows it. Selecting a model is not proof that the intended Aria runtime or agent used it: test a real request and a permitted tool workflow, verify logs without secrets, and confirm no fallback occurred. Native and Python runtimes may use different configuration routes; follow `docs/model-providers.md` for the runtime in use. Record request timestamp, requested and returned model, test outputs and result separately. This example has not been verified against a live account.

Official API: https://api.tokenfactory.nebius.com/docs
