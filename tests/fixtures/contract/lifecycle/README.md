# Tmux lifecycle consumer fixtures

These are verbatim public Tmux Session Contract v1 producer fixtures copied
from `rofi-tmux-plus/contracts/tmux-session-v1/fixtures` at the P5b boundary:
`external-rename.json`, `create-deferred.json`, `invalid-cwd.json`,
`session-exists.json`, `setup-rollback.json`, and `envelopes.json`.

They use only synthetic local descriptors.  Agent Plus consumer tests never
import a sibling checkout or execute a live tmux, SSH, Niri, or provider.
