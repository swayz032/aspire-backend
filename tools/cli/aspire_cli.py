#!/usr/bin/env python3
"""Aspire CLI — One-command local dev orchestration.

Commands:
  aspire up       Start all services (Postgres, Redis, Backend, Desktop, Admin, n8n, Prometheus)
  aspire down     Stop all services
  aspire health   Check health of all services
  aspire test     Run tests across all repos
  aspire logs     Tail logs from a service
  aspire reset-db Reset local database

Usage: python tools/cli/aspire_cli.py <command> [options]
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Resolve project root (2 levels up from tools/cli/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Service definitions
@dataclass
class Service:
    name: str
    cwd: str
    cmd: list[str]
    port: int
    health_url: Optional[str] = None
    env: Optional[dict[str, str]] = None


SERVICES: dict[str, Service] = {
    'backend': Service(
        name='Backend (FastAPI)',
        cwd=str(PROJECT_ROOT / 'backend' / 'orchestrator'),
        cmd=['python', '-m', 'uvicorn', 'src.aspire_orchestrator.server:app', '--host', '0.0.0.0', '--port', '8000', '--reload'],
        port=8000,
        health_url='http://localhost:8000/health',
    ),
    'desktop': Service(
        name='Desktop Server (Express)',
        cwd=str(PROJECT_ROOT / 'Aspire-desktop'),
        cmd=['npx', 'tsx', 'server/index.ts'],
        port=5000,
        health_url='http://localhost:5000/health',
    ),
    'admin': Service(
        name='Admin Portal (Vite)',
        cwd=str(PROJECT_ROOT / 'import-my-portal-main'),
        cmd=['npx', 'vite', '--port', '5173'],
        port=5173,
        health_url='http://localhost:5173',
    ),
}

# Docker services managed via docker-compose
DOCKER_SERVICES = ['postgres', 'redis', 'n8n', 'prometheus', 'grafana']


def color(text: str, code: str) -> str:
    """ANSI color wrapper."""
    if not sys.stdout.isatty():
        return text
    return f'\033[{code}m{text}\033[0m'


def green(text: str) -> str:
    return color(text, '32')


def red(text: str) -> str:
    return color(text, '31')


def yellow(text: str) -> str:
    return color(text, '33')


def bold(text: str) -> str:
    return color(text, '1')


def check_port(port: int) -> bool:
    """Check if a port is listening."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('localhost', port)) == 0


def check_health(url: str, timeout: int = 3) -> tuple[bool, str]:
    """Check a health endpoint."""
    try:
        import urllib.request
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            if status < 400:
                return True, f'{status} OK'
            return False, f'{status}'
    except Exception as e:
        return False, str(e)[:50]


def cmd_up(args: argparse.Namespace) -> int:
    """Start all services."""
    print(bold('Aspire: Starting services...'))
    print()

    # 1. Docker services
    compose_file = PROJECT_ROOT / 'docker-compose.yml'
    if compose_file.exists():
        print(f'  Starting Docker services...')
        result = subprocess.run(
            ['docker', 'compose', 'up', '-d'],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f'  {green("OK")} Docker services started')
        else:
            print(f'  {yellow("WARN")} Docker compose: {result.stderr[:100]}')
    else:
        print(f'  {yellow("SKIP")} No docker-compose.yml found')

    # 2. App services
    processes: dict[str, subprocess.Popen] = {}
    services_to_start = list(SERVICES.keys()) if not args.service else [args.service]

    for svc_name in services_to_start:
        svc = SERVICES.get(svc_name)
        if not svc:
            print(f'  {red("ERR")} Unknown service: {svc_name}')
            continue

        if check_port(svc.port):
            print(f'  {yellow("SKIP")} {svc.name} — already running on port {svc.port}')
            continue

        svc_cwd = Path(svc.cwd)
        if not svc_cwd.exists():
            print(f'  {yellow("SKIP")} {svc.name} — directory not found: {svc.cwd}')
            continue

        log_file = PROJECT_ROOT / '.aspire-logs' / f'{svc_name}.log'
        log_file.parent.mkdir(parents=True, exist_ok=True)

        env = {**os.environ, **(svc.env or {})}
        with open(log_file, 'w') as log:
            proc = subprocess.Popen(
                svc.cmd,
                cwd=svc.cwd,
                stdout=log,
                stderr=log,
                env=env,
            )
            processes[svc_name] = proc
            print(f'  {green("OK")} {svc.name} starting (PID {proc.pid}, port {svc.port})')

    if processes:
        print()
        print(f'  Logs: {PROJECT_ROOT / ".aspire-logs/"}')
        print(f'  Stop: python tools/cli/aspire_cli.py down')
        print()

        # Wait a moment then check health
        time.sleep(3)
        return cmd_health(args)

    return 0


def cmd_down(args: argparse.Namespace) -> int:
    """Stop all services."""
    print(bold('Aspire: Stopping services...'))

    # Kill app service processes by port
    for svc_name, svc in SERVICES.items():
        if check_port(svc.port):
            # Find and kill process on port (cross-platform)
            if sys.platform == 'win32':
                result = subprocess.run(
                    ['netstat', '-ano'],
                    capture_output=True, text=True,
                )
                for line in result.stdout.split('\n'):
                    if f':{svc.port}' in line and 'LISTENING' in line:
                        parts = line.split()
                        pid = parts[-1]
                        subprocess.run(['taskkill', '/F', '/PID', pid], capture_output=True)
                        print(f'  {green("OK")} {svc.name} stopped (PID {pid})')
                        break
            else:
                subprocess.run(
                    ['fuser', '-k', f'{svc.port}/tcp'],
                    capture_output=True,
                )
                print(f'  {green("OK")} {svc.name} stopped')
        else:
            print(f'  {yellow("SKIP")} {svc.name} — not running')

    # Docker services
    compose_file = PROJECT_ROOT / 'docker-compose.yml'
    if compose_file.exists():
        subprocess.run(
            ['docker', 'compose', 'down'],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
        )
        print(f'  {green("OK")} Docker services stopped')

    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Check health of all services."""
    print(bold('Aspire: Health Check'))
    print()

    all_healthy = True
    checks = [
        ('Postgres', 5432, None),
        ('Redis', 6379, None),
        ('Backend', 8000, 'http://localhost:8000/health'),
        ('Desktop', 5000, 'http://localhost:5000/health'),
        ('Admin Portal', 5173, 'http://localhost:5173'),
        ('n8n', 5678, 'http://localhost:5678/healthz'),
        ('Prometheus', 9090, 'http://localhost:9090/-/healthy'),
        ('Grafana', 3000, 'http://localhost:3000/api/health'),
        ('Ollama', 11434, 'http://localhost:11434'),
    ]

    for name, port, url in checks:
        port_up = check_port(port)
        if not port_up:
            print(f'  {red("DOWN")}  {name} (port {port})')
            all_healthy = False
            continue

        if url:
            healthy, detail = check_health(url)
            status = green('UP') if healthy else yellow('WARN')
            print(f'  {status}    {name} (port {port}) — {detail}')
            if not healthy:
                all_healthy = False
        else:
            print(f'  {green("UP")}    {name} (port {port})')

    print()
    if all_healthy:
        print(green('All services healthy'))
    else:
        print(yellow('Some services are down or unhealthy'))

    return 0 if all_healthy else 1


def cmd_test(args: argparse.Namespace) -> int:
    """Run tests across all repos."""
    print(bold('Aspire: Running Tests'))
    print()

    results: dict[str, tuple[int, str]] = {}

    # Backend tests (WSL)
    backend_dir = PROJECT_ROOT / 'backend' / 'orchestrator'
    if backend_dir.exists():
        print(f'  Running backend tests...')
        result = subprocess.run(
            ['wsl', '-d', 'Ubuntu-22.04', '-e', 'bash', '-c',
             'cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator && '
             'source ~/venvs/aspire/bin/activate && '
             'python -m pytest tests/ -q --tb=short 2>&1 | tail -5'],
            capture_output=True, text=True, timeout=300,
        )
        results['Backend'] = (result.returncode, result.stdout.strip())
        status = green('PASS') if result.returncode == 0 else red('FAIL')
        print(f'  {status} Backend: {result.stdout.strip().split(chr(10))[-1]}')

    # Desktop tests
    desktop_dir = PROJECT_ROOT / 'Aspire-desktop'
    if desktop_dir.exists():
        print(f'  Running desktop tests...')
        result = subprocess.run(
            ['npx', 'jest', '--ci', '--passWithNoTests'],
            cwd=str(desktop_dir),
            capture_output=True, text=True, timeout=120,
        )
        results['Desktop'] = (result.returncode, result.stdout.strip())
        status = green('PASS') if result.returncode == 0 else red('FAIL')
        last_line = result.stdout.strip().split('\n')[-1] if result.stdout.strip() else 'no output'
        print(f'  {status} Desktop: {last_line}')

    # Admin Portal tests
    admin_dir = PROJECT_ROOT / 'import-my-portal-main'
    if admin_dir.exists():
        print(f'  Running admin portal tests...')
        result = subprocess.run(
            ['npx', 'vitest', 'run', '--reporter=verbose'],
            cwd=str(admin_dir),
            capture_output=True, text=True, timeout=120,
        )
        results['Admin'] = (result.returncode, result.stdout.strip())
        status = green('PASS') if result.returncode == 0 else red('FAIL')
        last_line = result.stdout.strip().split('\n')[-1] if result.stdout.strip() else 'no output'
        print(f'  {status} Admin Portal: {last_line}')

    print()
    total = len(results)
    passed = sum(1 for rc, _ in results.values() if rc == 0)
    failed = total - passed

    if failed == 0:
        print(green(f'All {total} test suites passed'))
    else:
        print(red(f'{failed}/{total} test suites failed'))

    return 0 if failed == 0 else 1


def cmd_logs(args: argparse.Namespace) -> int:
    """Tail logs from a service."""
    service = args.service or 'backend'
    log_file = PROJECT_ROOT / '.aspire-logs' / f'{service}.log'

    if not log_file.exists():
        print(f'{red("ERR")} No log file for {service}. Start it first with: aspire up')
        return 1

    print(f'Tailing {service} logs (Ctrl+C to stop)...')
    try:
        subprocess.run(['tail', '-f', str(log_file)])
    except KeyboardInterrupt:
        pass
    return 0


def cmd_reset_db(args: argparse.Namespace) -> int:
    """Reset local database."""
    print(bold('Aspire: Resetting Database'))
    print(yellow('WARNING: This will drop and recreate all tables.'))

    confirm = input('Type "yes" to confirm: ')
    if confirm.lower() != 'yes':
        print('Aborted.')
        return 1

    result = subprocess.run(
        ['wsl', '-d', 'Ubuntu-22.04', '-e', 'bash', '-c',
         'cd /mnt/c/Users/tonio/Projects/myapp/backend/orchestrator && '
         'source ~/venvs/aspire/bin/activate && '
         'python -m aspire_orchestrator.scripts.reset_db'],
        capture_output=True, text=True,
    )

    if result.returncode == 0:
        print(green('Database reset complete'))
    else:
        print(red(f'Reset failed: {result.stderr[:200]}'))

    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        prog='aspire',
        description='Aspire CLI — Local dev orchestration',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    # up
    up_parser = subparsers.add_parser('up', help='Start all services')
    up_parser.add_argument('service', nargs='?', help='Start specific service')

    # down
    subparsers.add_parser('down', help='Stop all services')

    # health
    subparsers.add_parser('health', help='Check health of all services')

    # test
    test_parser = subparsers.add_parser('test', help='Run tests across all repos')
    test_parser.add_argument('--repo', help='Test specific repo (backend, desktop, admin)')

    # logs
    logs_parser = subparsers.add_parser('logs', help='Tail service logs')
    logs_parser.add_argument('service', nargs='?', default='backend', help='Service name')

    # reset-db
    subparsers.add_parser('reset-db', help='Reset local database')

    args = parser.parse_args()

    commands = {
        'up': cmd_up,
        'down': cmd_down,
        'health': cmd_health,
        'test': cmd_test,
        'logs': cmd_logs,
        'reset-db': cmd_reset_db,
    }

    return commands[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
