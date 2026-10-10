# Agent tooling

For v0.1.0 preserve the captured operational runtime. Do not mix quota fixes or other behavior changes into the import. Publish fixes before local verification. Required verification is `make verify` through the local managed validation runner. Keep state, tokens, personal settings and conversations out of Git. Known quota regressions must remain explicit; new failures must fail verification.

The current managed plan parser accepts actual repository scripts. Declare verification with `argv: ["python3.13", "tools/manage.py", "verify"]` and installation with `argv: ["python3.13", "tools/manage.py", "install"]`; these are the same implementations used by the Makefile targets. Run them through `caller-pr-gates validate --background` for an owned PR. The documented `make install` command remains available for the human operator.
