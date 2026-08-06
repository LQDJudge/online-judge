# Bridge Testing

This document is the standard verification path for changes under `judge/bridge/`
or `judge/judgeapi.py`.

## Prerequisites

Run commands from `online-judge/` unless noted otherwise:

```bash
source ../dmojsite/bin/activate
```

The local database should contain at least:

- a judge row whose name/key matches `../judge.yml`
- language `PY3`
- user `admin`

Check quickly:

```bash
python3 manage.py shell -c "from judge.models import Language, Profile; print(Language.objects.filter(key='PY3').exists(), Profile.objects.filter(user__username='admin').exists())"
```

## Unit Tests

Run focused bridge tests:

```bash
python3 manage.py test judge.tests.test_judge_list
python3 manage.py test judge.tests.test_bridge_reliability
python3 manage.py check
```

## Local Smoke Test

Start the bridge:

```bash
python3 manage.py runbridged
```

In another terminal from the `LQDOJ/` directory, start one judge:

```bash
source dmojsite/bin/activate
dmoj -c judge.yml localhost
```

Confirm the bridge sees the judge:

```bash
python3 manage.py bridge_status
```

For a debug snapshot of bridge memory state:

```bash
python3 manage.py bridge_status --detail
python3 manage.py bridge_status --detail --json
```

Detailed status includes connected judges, active submissions, active
validations, queued work by effective priority, and the per-user concurrency
cap state. It reports source size but intentionally does not print raw
submission source. Add `--include-problems` when you need every problem code
advertised by each judge.

Submit one real solution through the Django-to-bridge path:

```bash
python3 manage.py stress_bridge --problem auto --count 1 --keep-submissions
```

Expected result:

- `bridge_status` reports at least one connected judge.
- The stress command finishes without timeout.
- The single submission reaches a terminal status. The default source is a tiny
  wrong-answer program because stress testing is about dispatch/recovery speed.
  Use an explicit problem and `--source-mode ac` only when you have a known
  matching source.
- `bridge_status` reports zero active submissions after completion.

If `stress_bridge` times out, it keeps the submissions it created even without
`--keep-submissions`. Do not delete submissions that judges may still be
grading; late packets for deleted submissions can create noisy bridge errors and
database integrity failures that obscure the reliability issue being tested.

## Stress Test

Run a burst of submissions through the real bridge and judge:

```bash
python3 manage.py stress_bridge \
    --problem auto \
    --language PY3 \
    --count 200 \
    --concurrency 20
```

Acceptance criteria:

- Bridge remains responsive to `python3 manage.py bridge_status` while load is running.
- All submissions reach terminal status before timeout.
- Queue depth returns to zero after the run.
- No unexpected `judge.bridge` exceptions appear in bridge logs.

For multiple local judges, start additional judge processes from `LQDOJ/`:

```bash
dmoj -c judge2.yml localhost
dmoj -c judge3.yml localhost
```

Then assert the bridge is actually using at least two workers:

```bash
python3 manage.py stress_bridge \
    --problem auto \
    --language PY3 \
    --count 6 \
    --concurrency 2 \
    --expect-judges 2 \
    --expect-min-active 2
```

This is stronger than the single-judge smoke test: it proves the queue can feed
more than one connected judge and gives a realistic baseline before injecting a
fault into one judge.

Use `--source-mode slow-wa` when you need an active-grading window for manual
fault injection. It intentionally sleeps briefly before printing a wrong answer,
so the bridge holds active submissions long enough for `kill`, `SIGSTOP`, or
fault-proxy commands to land.

Use `--judge-id <name>` to target one online judge. By default, `stress_bridge`
preflights that the target judge exists, is online, and advertises the selected
problem/language. Pass `--skip-compat-check` only when you intentionally want to
observe queued work for an unavailable target.

## Docker Judge Path

If the direct local judge cannot run, use the Docker judge scripts:

```bash
.docker/bridge/run.sh
export PROBLEMS_DIR=/path/to/problems
.docker/judge/start_judge.sh judge1
```

Then run the same `bridge_status` and `stress_bridge` checks.

## Network Fault Tests

The fault proxy sits between judge-server and bridge:

```text
judge -> fault proxy -> bridge
        127.0.0.1:19999 -> 127.0.0.1:9999
```

Start bridge normally on port `9999`, then start the proxy:

```bash
python3 manage.py bridge_fault_proxy \
    --listen 127.0.0.1:19999 \
    --upstream 127.0.0.1:9999
```

Start judge against the proxy from `LQDOJ/`:

```bash
dmoj -c judge.yml 127.0.0.1 -p 19999
```

For a stronger recovery test, run one direct judge and one proxied judge:

```bash
dmoj -c judge.yml localhost
dmoj -c judge2.yml 127.0.0.1 -p 19999
```

Confirm both are online:

```bash
python3 manage.py bridge_status --detail
```

Run stress:

```bash
python3 manage.py stress_bridge \
    --problem auto \
    --count 10 \
    --concurrency 2 \
    --timeout 300 \
    --expect-judges 2 \
    --expect-min-active 2
```

Inject faults from another terminal while stress is active.

Hard close all proxied judge connections:

```bash
python3 manage.py bridge_fault_proxy_control close
```

Cut a connection after a partial upstream packet, useful for packet-body EOF bugs:

```bash
python3 manage.py bridge_fault_proxy_control cut-after-bytes 12 upstream
```

Blackhole judge-to-bridge traffic while keeping sockets open:

```bash
python3 manage.py bridge_fault_proxy_control blackhole upstream
```

Blackhole bridge-to-judge traffic:

```bash
python3 manage.py bridge_fault_proxy_control blackhole downstream
```

Add latency:

```bash
python3 manage.py bridge_fault_proxy_control latency 500
```

Restore normal forwarding:

```bash
python3 manage.py bridge_fault_proxy_control restore
```

Fault-test acceptance criteria:

- Bridge stays responsive to `bridge_status`.
- Broken judge connections are removed from active dispatch.
- Remaining or restarted judges can receive new submissions without restarting bridge.
- Queue depth does not grow permanently after the fault is removed.
- The stress command reaches terminal status for all submissions and reports
  zero active/queued submissions at the end.

## Process Fault Tests

Pause a judge while stress is running. First start bridge and a direct judge,
then start a small stress run:

```bash
python3 manage.py stress_bridge \
    --problem auto \
    --count 3 \
    --concurrency 1 \
    --timeout 180 \
    --expect-judges 1 \
    --expect-min-active 1
```

When `bridge_status --detail` shows an active submission, pause the judge:

```bash
pgrep -af "DMOJ Judge judge1"
kill -STOP <pid>
python3 manage.py bridge_status --detail
sleep 10
kill -CONT <pid>
```

Or kill the judge entirely:

```bash
kill <pid>
```

Expected result:

- Bridge remains responsive.
- `bridge_status --detail` reflects the active submission and queued work
  while the judge process is stopped.
- The stress command finishes after the judge resumes.
- Starting the judge again reconnects without restarting bridge.

## Realistic Failure Simulation Matrix

Use this matrix when bridge changes touch sockets, judge lifecycle, queue
ownership, retry logic, or admin/debug tooling. Prefer real bridge + real judge
simulation where possible, and use unit tests for races that are hard to make
deterministic with live processes.

| Scenario | Why it happens | How to simulate | Expected behavior |
| --- | --- | --- | --- |
| Judge socket closes cleanly during active grading | judge process exits, host reboot, container restart | Run two judges, start `stress_bridge`, then `kill <proxied-or-direct-judge-pid>` | Bridge removes the judge, requeues owned work, remaining judge drains queue, final active/queued counts are zero |
| Judge connection breaks mid-packet | network reset, proxy/LB closes a connection while packet body is in flight | Start judge through `bridge_fault_proxy`, then `bridge_fault_proxy_control cut-after-bytes 12 upstream` | Bridge handler disconnects instead of spinning; bridge remains responsive |
| Judge-to-bridge traffic blackholes | firewall drop or half-open TCP path from judge to bridge | `bridge_fault_proxy_control blackhole upstream` during stress | Bridge remains responsive; no permanent queue growth after fault is removed or judge reconnects |
| Bridge-to-judge traffic blackholes | bridge can read judge packets but judge cannot receive work/terminations | `bridge_fault_proxy_control blackhole downstream` during stress | The bridge-side acknowledgement watchdog closes the failed judge after no ack; work is requeued and remaining/reconnected judges continue |
| Proxied judge hard close during active grading | broken pipe like production incident | Two judges, one through proxy, `stress_bridge --expect-judges 2 --expect-min-active 2`, then `bridge_fault_proxy_control close` | Broken judge is removed; queue drains on surviving judge |
| High latency between bridge and judge | overloaded network, distant judge host | `bridge_fault_proxy_control latency 500` during stress | Throughput drops, but bridge status remains responsive and queue drains |
| Judge process pauses without disconnecting | host CPU starvation, container frozen, `SIGSTOP` | `kill -STOP <judge-pid>`, check `bridge_status --detail`, then `kill -CONT <judge-pid>` | Status shows active/queued work while paused; bridge remains responsive; run completes after resume |
| Judge process killed and restarted with same id | crash loop or deploy restart | Kill `judge1` during stress, then run `dmoj -c judge.yml localhost` again | Old judge is removed, same-name replacement is registered, owned work is not lost |
| Duplicate same-name judge overlaps old connection | reconnect before old socket cleanup finishes | Start two `dmoj -c judge.yml localhost` processes | Only one `judge1` remains dispatchable; stale connection cannot receive new work |
| Stale packet arrives after reassignment | old socket emits late completion after bridge requeued work | Unit test: old judge completes submission now owned by new judge | Bridge ignores stale completion and preserves new owner |
| Validation disconnect overlaps submission id `1` | validation uses boolean working sentinel, `True == 1` in Python | Unit test: submission id `1` active on judge A, validation active on judge B, remove B | Submission id `1` remains mapped to judge A |
| Manual admin disconnect hits broken socket | admin/API disconnect request while judge pipe is already broken | Make fake judge disconnect raise in unit test; optionally close proxied judge then call `disconnect_judge` | Dead judge is removed/requeued instead of leaving stale memory state |
| Problem update broadcast hits broken socket | admin updates problem data while a judge connection is stale | With proxied judge broken, call `python3 manage.py shell -c "from judge.judgeapi import notify_problem_update; notify_problem_update()"` | Broken judge is removed; broadcast does not wedge bridge |
| Django-to-bridge request times out | bridge is hung, overloaded, or unreachable from web process | Stop bridge or point `BRIDGED_DJANGO_CONNECT` at a blackhole/closed port, then call `bridge_status` or submit | Web-side call fails within `BRIDGED_DJANGO_TIMEOUT_SECONDS`; request does not hang indefinitely |
| DB connection goes stale in judge packet handling | MySQL idle timeout or restart | Unit test `OperationalError(2006)` on `supported-problems`; for live test, restart MySQL while judge reconnects if safe locally | Retryable packet updates retry once after closing stale DB connection |
| DB connection goes stale during handshake DB update | MySQL idle timeout exactly as judge authenticates | Unit test `_connected()` through `handshake-connected` retry wrapper | Judge does not remain partially registered only in memory due to one stale DB connection |
| Queue surge with two judges | contest spike, batch rejudge | `stress_bridge --problem auto --count 30 --concurrency 4 --poll-interval 0.25 --expect-judges 2 --expect-min-active 2` | Both judges become active, all submissions finish, final queue is zero |
| Queue surge plus one judge failure | contest spike plus worker crash | Same stress as above, but close/kill one judge after `max active` reaches 2 | Remaining judge drains queue; no stale active entry remains |
| Targeted judge unavailable | admin submits to a specific judge that is offline or dies | `stress_bridge --judge-id missing-judge --count 1`; use `--skip-compat-check` only when intentionally observing stuck targeted work | Stress preflight rejects missing/incompatible targets; production work is not sent to incompatible judges |
| Unsupported problem/language | judge problem glob missing `init.yml`, runtime disabled | Use explicit problem/language not advertised by any online judge | `stress_bridge` preflight fails clearly instead of creating permanently queued work |
| Large or malformed packet | corrupted judge, buggy proxy, hostile client | Unit test packet size/decompression errors; optionally send malformed TCP packet manually | Bridge disconnects/logs and does not crash |

## Local Simulation Results

Last run: 2026-08-06, using one direct judge and one proxied judge.

| Scenario | Coverage | Result |
| --- | --- | --- |
| Judge socket closes cleanly during active grading | Live: killed `judge1` during `stress_bridge --source-mode slow-wa` and restarted `judge1` | Passed: work requeued, replacement connected, final queue/active counts were zero |
| Judge connection breaks mid-packet | Live: proxy `cut-after-bytes 12 upstream` before a fresh proxied judge handshake | Passed: malformed handshake disconnected/retried and bridge stayed responsive |
| Judge-to-bridge traffic blackholes | Live: proxy `blackhole upstream` during slow stress, then restored before final packets | Passed: all submissions finished and final queue/active counts were zero |
| Bridge-to-judge traffic blackholes | Live: proxy `blackhole downstream` before dispatch, without manually closing the socket | Passed: the existing acknowledgement watchdog logged `Judge failed to acknowledge submission`, closed the failed judge, requeued lost active work, and final queue/active counts were zero |
| Proxied judge hard close during active grading | Live: proxy `close` during two-judge slow stress | Passed: broken judge removed, surviving judge drained queue, final queue/active counts were zero |
| High latency between bridge and judge | Live: proxy `latency 500` during two-judge slow stress | Passed: throughput dropped, detailed status stayed responsive, final queue/active counts were zero |
| Judge process pauses without disconnecting | Live: `SIGSTOP`/`SIGCONT` direct judge during active slow stress | Passed: detail status showed active work while paused and run completed after resume |
| Judge process killed and restarted with same id | Live: killed direct `judge1` during active slow stress, then started `dmoj -c judge.yml localhost` | Passed: old work requeued and replacement `judge1` registered without bridge restart |
| Duplicate same-name judge overlaps old connection | Live: started a second `dmoj -c judge.yml localhost` while `judge1` was online | Passed: bridge detail reported only one dispatchable `judge1`; duplicate processes dueled until one was stopped |
| Stale packet arrives after reassignment | Unit: old fake judge completed work now owned by a new fake judge | Passed |
| Validation disconnect overlaps submission id `1` | Unit: validation boolean sentinel plus active submission id `1` | Passed |
| Manual admin disconnect hits broken socket | Unit for broken socket; live normal admin disconnect of `judge2` | Passed: live disconnect removed judge and left bridge memory empty |
| Problem update broadcast hits broken socket | Live: closed proxied socket, then called `notify_problem_update()` | Passed: bridge stayed responsive and removed the broken proxied judge |
| Django-to-bridge request times out | Live: dummy socket accepted and never responded with `BRIDGED_DJANGO_TIMEOUT_SECONDS=1` | Passed: `bridge_status()` raised `TimeoutError` in about one second |
| DB connection goes stale in judge packet handling | Unit: retryable `OperationalError(2006)` on packet handling | Passed |
| DB connection goes stale during handshake DB update | Unit: retry wrapper around connected-handshake DB update | Passed |
| Queue surge with two judges | Live: `stress_bridge --count 30 --concurrency 4 --expect-judges 2 --expect-min-active 2` | Passed: all submissions finished, max active was two, final queue was zero |
| Queue surge plus one judge failure | Live: slow stress plus proxied hard close | Passed: surviving judge drained requeued work |
| Targeted judge unavailable | Live: `stress_bridge --judge-id missing-judge --count 1` | Passed: command failed preflight before creating submissions |
| Targeted online judge | Live: `stress_bridge --judge-id judge1 --count 1` | Passed: targeted submission finished and bridge memory returned to zero |
| Unsupported problem/language | Live: `stress_bridge --language RUBY --count 1` | Passed: command failed preflight before creating submissions |
| Large or malformed packet | Live: sent invalid zlib payload and partial packet directly to bridge TCP port | Passed: bridge stayed responsive with clean memory |

## Before Merging Bridge Changes

Run at minimum:

```bash
python3 manage.py test judge.tests.test_judge_list
python3 manage.py test judge.tests.test_bridge_reliability
python3 manage.py check
python3 manage.py bridge_status
python3 manage.py stress_bridge --count 50 --concurrency 10
```

For socket, queue, or reconnect changes, also run at least one fault-proxy test.
For reliability changes that claim recovery from stalled or broken judges, run
both a network fault test and a process pause/resume test.
