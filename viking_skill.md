# OpenViking Bridge — Skill Instructions

You have access to two tools that query the OpenViking context database. Use them to retrieve project knowledge before answering questions about the codebase, architecture, configuration, or implementation details.

## Tool Selection Rules

### `query_openviking(context_id)`

Use when you have a **specific Viking URI** and need to read its content.

- The `context_id` parameter is a Viking URI (e.g. `viking://resources/example_app/AUTH_QUICK_REFERENCE`).
- Returns the full text content (L2), then overview (L1), then abstract (L0).
- Use this when a previous search result gave you a URI you want to expand.

**Example 1 — Reading a known resource:**
> User: "Show me the authentication quick reference."
> Action: `query_openviking("viking://resources/example_app/AUTH_QUICK_REFERENCE")`

**Example 2 — Expanding a search result:**
> A previous `send_to_viking` call returned a hit at `viking://resources/example_app/CORS_AND_ENV_IMPLEMENTATION_GUIDE`. 
> Action: `query_openviking("viking://resources/example_app/CORS_AND_ENV_IMPLEMENTATION_GUIDE")`

**Example 3 — Discovering core services from project root:**
> User: "List the existing services in `example_app`."
> Action: `query_openviking("viking://resources/example_app/")`

### `send_to_viking(prompt)`

Use when you need to **search** for relevant context using a natural-language query.

- Performs semantic search across all indexed resources.
- Returns ranked results with URIs, scores, and abstracts.
- Use this as the **first step** when you don't know which URI to read.

**Example 1 — Discovering relevant context:**
> User: "How does the order status update flow work?"
> Action: `send_to_viking("order status update flow")`

**Example 2 — Finding configuration details:**
> User: "What CORS settings are configured for the webhook service?"
> Action: `send_to_viking("CORS configuration webhook service")`

## Decision Flow

```
User asks a question about the project
  │
  ├─ You know the exact Viking URI? → query_openviking(uri)
  │
  ├─ You need to discover relevant context? → send_to_viking(query)
  │     └─ Got a useful URI in results? → query_openviking(uri) for full content
  │
  └─ Both tools fail or return empty? → Fall back to Knowledge base
```

## Fallback Behavior

If a live OpenViking call fails (timeout, auth error, server unreachable):

1. State clearly: "Live OpenViking query failed, checking Knowledge base."
2. Search the attached OpenWebUI Knowledge base — synced entries are named `viking_context_<path>.md`.
3. If Knowledge base also has no relevant entry, state that no project context was found and answer from general knowledge with an explicit disclaimer.

## Citation Rules

Always cite the source of retrieved information:

- **From live OpenViking:** Cite the Viking URI.
  > *Source: `viking://resources/example_app/AUTH_QUICK_REFERENCE` (live)*

- **From Knowledge base fallback:** Cite the synced file name.
  > *Source: `viking_context_resources_example_app_AUTH_QUICK_REFERENCE.md` (knowledge base)*

- **No source found:** State explicitly.
  > *No project context found. Answering from general knowledge.*

## Do Not

- Guess Viking URIs. Search first with `send_to_viking`, then read with `query_openviking`.
- Omit citations. Every factual claim from OpenViking must have a source.
- Silently skip fallback. If live calls fail, always try the Knowledge base before giving up.

## Output Quality

When the user asks for service names/modules/components, prefer a structured response:

- A short bullet list or table with service name + URI
- Keep naming exactly as found in OpenViking
- Include source citation(s) at the end
