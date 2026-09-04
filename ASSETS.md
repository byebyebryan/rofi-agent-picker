# Provider icons

The picker bundles small, monochrome SVGs so Rofi does not depend on the
current host's icon theme.  The provider shapes preserve the source geometry;
only the fill is set to the picker foreground color (`#e0e2e8`) for legibility.

| File | Source | License and usage |
| --- | --- | --- |
| `rofi_agent_plus/assets/providers/claude.svg` | [Simple Icons, `claudecode.svg`](https://github.com/simple-icons/simple-icons/blob/1bd24ad0645f18ec68b17a087daa5649644bd303/icons/claudecode.svg) | [CC0 1.0](https://github.com/simple-icons/simple-icons/blob/1bd24ad0645f18ec68b17a087daa5649644bd303/LICENSE.md); Claude and Claude Code remain Anthropic trademarks. |
| `rofi_agent_plus/assets/providers/opencode.svg` | [Simple Icons, `opencode.svg`](https://github.com/simple-icons/simple-icons/blob/1bd24ad0645f18ec68b17a087daa5649644bd303/icons/opencode.svg) | [CC0 1.0](https://github.com/simple-icons/simple-icons/blob/1bd24ad0645f18ec68b17a087daa5649644bd303/LICENSE.md); OpenCode remains its owner's trademark. |
| `rofi_agent_plus/assets/providers/codex.svg` | [OpenAI Cookbook, `openai-logomark.svg`](https://github.com/openai/openai-cookbook/blob/4a85c3018d20ceef48bf7549450c567896501bf9/examples/agents_sdk/deployment_manager/frontend/src/openai-logomark.svg) | Source repository [MIT license](https://github.com/openai/openai-cookbook/blob/4a85c3018d20ceef48bf7549450c567896501bf9/LICENSE); the OpenAI knot is an OpenAI trademark subject to its [brand guidelines](https://openai.com/brand/). |

These icons identify the provider associated with a session.  They do not
imply sponsorship, endorsement, or affiliation with this project.  The
bundled `generic.svg` is a local fallback for an unrecognized or unavailable
provider asset.
