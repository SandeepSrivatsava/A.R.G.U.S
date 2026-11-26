---
name: clean-code
description: How to change code well, including Argus's own code with code_fix. Use when fixing a bug, a crash, an error, or writing or changing code or scripts.
triggers: fix, bug, crash, code, code_fix, script, error, broken, refactor, patch
---
Guidelines for changing code (after Andrej Karpathy's notes on how coding models go wrong):
1. Think first. Read the code around the problem and say what you believe causes it before editing. If two
   readings are possible, pick the one the logs support, or ask.
2. Smallest fix that works. Change only what the problem needs. No new abstractions, options or "while I'm
   here" clean-ups; no reformatting of lines you didn't need to touch.
3. Match the file. Same names, style and comments as the code around it.
4. Prove it. Add or adjust a test that fails without the fix and passes with it, then run the tests. If a
   test failed before your change too, say so instead of "fixing" it.
5. Say what you changed in one or two plain sentences, and what you didn't check.
