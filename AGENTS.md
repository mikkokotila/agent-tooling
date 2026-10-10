# Agent tooling

Preserve original capture under capture/ and tag v0.1.0. Authorized repairs to issue #3 evolve the committed release/runtime-manifest.json; never hide new failures or weaken gates. Publish fixes before local verification. Required verification is tools/manage.py verify through the managed runner. Keep state, tokens, personal settings and chats out of Git. Install each independently verified repair stage before advancing to the next.

Declare verification with argv ["python3.13", "tools/manage.py", "verify"] and installation with ["python3.13", "tools/manage.py", "install"]. Run through caller-pr-gates validate --background for owned PRs. make install uses the same implementation.
