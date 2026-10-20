# gitlab-registry-mirror

Keep this repository limited to a reviewed image catalog, tagged registry copies, the webhook request the low NiFi acts on, and tars carried by hand: `sync`, `request`, `export`, `import`. Jobs need Python and skopeo, not Docker or privileged execution.

Read `.agent/PENDING.md` and `.agent/SKILL.md` when present. Never commit `.agent/`, credentials, ledgers, staging directories or tars.

Validate changes with `python3 -m unittest discover -s tests -v`, `ruff check mirror.py tests`, and `yamllint -d relaxed` on `.gitlab-ci.yml` and the GitHub workflow. A unit test is not a transfer: `tests/e2e.py` runs sync, request (to a listener it starts), export and import against real registries (log in with skopeo, set E2E_LOW_REGISTRY, E2E_HIGH_REGISTRY, E2E_TLS_VERIFY), and `tests/matrix.py <registry/repo:tag>...` carries real source references. Point them only at test registries.

The NiFi processors live in github.com/djlongy/nifi-container-registry-processors.

Keep the README under 140 lines and QUICKSTART under 40. The README leads with Usage (copy one image across by hand, then mirror the catalog), then the Commands table listing every command and flag, then Environment.
