"""Run a browser smoke test against freshly started fixture processes.

The fixture app (tests/browser_server.py) keeps state for the life of its
process: prices injected through /internal/test-quote, and the application's
own process-wide singletons (the rate limiter, the ranking cache, the SSE
quote hub, provider caches). Production code has no reset path for those, and
must not get one, so each run starts a new browserweb process, and an empty
browserredis where the compose file has one, before the browser container.
The database is kept; the smoke creates every account it relies on.

    python3 scripts/browser_smoke.py -p paper-sse-test -f compose.sse-test.yaml
    SSE_TEST_ENABLED=false python3 scripts/browser_smoke.py -p paper-sse-rollback -f compose.sse-test.yaml
    python3 scripts/browser_smoke.py -f compose.yaml -f compose.browser.yaml
    python3 scripts/browser_smoke.py -p paper-sse-test -f compose.sse-test.yaml -- python browser_restart_smoke.py
"""
import argparse
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('-p', '--project')
    parser.add_argument('-f', '--file', action='append', required=True)
    parser.add_argument('--no-build', action='store_true', help='use the existing browser image')
    parser.add_argument('command', nargs='*', help='command for the browser container (after --)')
    args = parser.parse_args()
    compose = ['docker', 'compose'] + (['-p', args.project] if args.project else []) + [a for f in args.file for a in ('-f', f)]
    services = subprocess.run(compose + ['config', '--services'], check=True, capture_output=True, text=True).stdout.split()
    fresh = [name for name in ('browserweb', 'browserredis') if name in services]
    subprocess.run(compose + ['rm', '--stop', '--force'] + fresh, check=True)
    run = compose + ['run', '--rm'] + ([] if args.no_build else ['--build']) + ['browser'] + args.command
    return subprocess.call(run)


if __name__ == '__main__':
    sys.exit(main())
