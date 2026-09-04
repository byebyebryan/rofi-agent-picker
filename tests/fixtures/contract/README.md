# Contract consumer fixtures

These small, generic fixtures are copied/adapted from the public Host Mesh v1
and Tmux Session v1 producer conformance shapes in the sibling Plus projects.
They intentionally use only synthetic logical hosts and routes.  Consumer
tests execute deterministic fakes against these JSON records; they never read
or import a sibling checkout.
