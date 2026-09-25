# WINDOWS NOTES

This desk runs on Lando's Windows PC. These notes override anything in BUILD.md that assumes macOS or Linux.

## Python
- Install Python 3.12 from python.org with "Add python.exe to PATH" checked.
- Pick the interpreter by testing it. Windows often has a python3 stub that opens the Microsoft Store instead of running Python. Prefer the py launcher (py -3) if python3 fails.
- Use that exact command everywhere before lock-down: prompts/, CLAUDE.md, setup/settings.template.json, and the scheduled task.

## Hooks: prove they fire
- A hook command that fails to start blocks nothing. Confirm guard.py and capture.py actually run on this machine.
- If $CLAUDE_PROJECT_DIR does not expand in the hook command, use the absolute path to the scripts instead.
- The Phase 2 unapproved-order test is the proof. It must be denied by the guard, with the denial logged in state/events.jsonl.

## runner.py
- Find the executable with shutil.which("claude"). An npm install on Windows is claude.cmd, which subprocess cannot start by bare name.
- Send the prompt text on stdin, not as a command-line argument. Multi-line arguments break under cmd.exe quoting.
- On timeout, kill the whole tree: taskkill /T /F /PID <pid>.

## Files
- Locks: msvcrt.locking. Atomic writes: os.replace, which overwrites on Windows where os.rename fails.
- Lock-down: attrib +R /S /D on config, scripts, prompts, reference, .claude, CLAUDE.md, and BUILD.md. Then prove a headless run still cannot edit them.
- Keep LF line endings and UTF-8. Check that git did not convert the prompts to CRLF.

## Scheduler and power
- Task Scheduler: repeat every 5 minutes indefinitely. Action: the chosen interpreter running scripts\watcher.py tick, starting in the trend-desk folder. "Run only when user is logged on" so it can reach the Claude Code login. "Do not start a new instance" if one is still running. Stop the task if it runs longer than 30 minutes.
- Sleep: Never, when plugged in.
- Windows Update: set active hours to cover 5 PM to 8 PM Mountain, the daily run window. An update restart logs Lando out, and the desk stops until he logs back in. Stops at Robinhood keep protecting open positions, and the missed-run alert tells him.
