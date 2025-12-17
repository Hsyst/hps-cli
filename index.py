# hps_cli.py - SISTEMA DE COMUNICAÇÃO ENTRE INSTÂNCIAS CLI OTIMIZADO E CONECTADO
import asyncio
import aiohttp
import socketio
import json
import os
import hashlib
import base64
import time
import threading
import uuid
from pathlib import Path
import mimetypes
import logging
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidSignature
import tempfile
import sqlite3
import ssl
import struct
import secrets
import sys
import argparse
import getpass
from datetime import datetime
import shutil
import platform
import subprocess
import re
from typing import Dict, List, Optional, Any, Callable
import signal
import pickle
import atexit
import grp
import pwd
import stat
import fcntl
import multiprocessing
import setproctitle
import traceback
import select

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("HPS-CLI")

class ControllerFileMonitor:
    def __init__(self, client_core, display):
        self.client_core = client_core
        self.display = display
        self.controller_file = os.path.join(os.path.expanduser("~"), ".hps_cli", "controller_hpscli")
        self.controller_dir = os.path.dirname(self.controller_file)
        self.logs_dir = os.path.join(self.controller_dir, "logs")
        self.is_monitoring = False
        self.monitor_thread = None
        self.last_modified = 0
        self.command_lock = threading.Lock()
        self.pid_file = os.path.join(self.controller_dir, "controller.pid")
        self.active_commands = {}
        self.loop = None
        self.connection_state = None

        os.makedirs(self.controller_dir, exist_ok=True)
        os.makedirs(self.logs_dir, exist_ok=True)

        self.current_command_id = None
        self.current_log_file = None
        self.command_callbacks = {}
        self.cleanup_old_files()

    def cleanup_old_files(self):
        try:
            if os.path.exists(self.pid_file):
                with open(self.pid_file, 'r') as f:
                    old_pid = int(f.read().strip())
                    try:
                        os.kill(old_pid, 0)
                        os.kill(old_pid, signal.SIGTERM)
                    except:
                        pass
                os.remove(self.pid_file)

            if os.path.exists(self.controller_file):
                try:
                    content = self.read_controller_file()
                    if content.startswith(self.logs_dir):
                        log_file = content
                        if os.path.exists(log_file):
                            os.remove(log_file)
                except:
                    pass
                try:
                    os.remove(self.controller_file)
                except:
                    pass

            for f in os.listdir(self.logs_dir):
                file_path = os.path.join(self.logs_dir, f)
                if os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                    except:
                        pass
        except Exception as e:
            self.display.print_error(f"Cleanup error: {e}")

    def read_controller_file(self):
        try:
            with open(self.controller_file, 'r') as f:
                return f.read().strip()
        except:
            return ""

    def write_controller_file(self, content):
        try:
            with open(self.controller_file, 'w') as f:
                f.write(content)
        except Exception as e:
            self.display.print_error(f"Write controller error: {e}")

    def start_monitoring(self):
        if self.is_monitoring:
            return

        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))

        self.is_monitoring = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        self.display.print_success("Controller file monitor started")

    def stop_monitoring(self):
        self.is_monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        self.cleanup_old_files()

    def _monitor_loop(self):
        self.display.print_info(f"Monitoring controller file: {self.controller_file}")

        while self.is_monitoring:
            try:
                if not os.path.exists(self.controller_file):
                    time.sleep(0.1)
                    continue

                current_modified = os.path.getmtime(self.controller_file)
                if current_modified > self.last_modified:
                    self.last_modified = current_modified

                    with self.command_lock:
                        content = self.read_controller_file()

                        if content and not content.startswith(self.logs_dir):
                            command_content = content
                            self.display.print_info(f"Received command from controller: {command_content}")

                            command_id = str(uuid.uuid4())
                            log_file = os.path.join(self.logs_dir, f"{command_id}.log")

                            self.write_controller_file(log_file)

                            threading.Thread(
                                target=self._execute_command_with_log,
                                args=(command_id, log_file, command_content),
                                daemon=True
                            ).start()

            except Exception as e:
                self.display.print_error(f"Monitor error: {e}")

            time.sleep(0.1)

    def _write_log_status(self, log_file, status, message=""):
        try:
            with open(log_file, 'w') as f:
                f.write(f"{status}\n")
                if message:
                    f.write(f"{message}\n")
        except Exception as e:
            self.display.print_error(f"Write log error: {e}")

    def _append_log_result(self, log_file, result):
        try:
            with open(log_file, 'a') as f:
                f.write(f"{result}\n")
        except Exception as e:
            self.display.print_error(f"Append log error: {e}")

    def _read_log_file(self, log_file):
        try:
            if not os.path.exists(log_file):
                return None
            with open(log_file, 'r') as f:
                lines = f.readlines()
                if len(lines) >= 1:
                    status = lines[0].strip()
                    message = lines[1].strip() if len(lines) > 1 else ""
                    result = lines[2].strip() if len(lines) > 2 else ""
                    return {
                        'status': status,
                        'message': message,
                        'result': result
                    }
                return None
        except Exception as e:
            self.display.print_error(f"Read log error: {e}")
            return None

    def _execute_command_with_log(self, command_id, log_file, command_content):
        try:
            self.active_commands[command_id] = True
            self._write_log_status(log_file, "1", "Command execution started")

            result = self._execute_controller_command(command_content, log_file)

            if result['success']:
                self._write_log_status(log_file, "1", result['output'])
                self._append_log_result(log_file, "1")
            else:
                self._write_log_status(log_file, "0", result['output'])
                self._append_log_result(log_file, "0")

            self.active_commands[command_id] = False

        except Exception as e:
            self.display.print_error(f"Command execution error: {e}")
            self._write_log_status(log_file, "0", str(e))
            self._append_log_result(log_file, "0")

    def _execute_controller_command(self, command_content, log_file):
        try:
            parts = command_content.strip().split()
            if not parts:
                return {'success': False, 'output': 'Empty command'}

            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []

            if command in self.client_core.command_handlers:
                import io
                from contextlib import redirect_stdout, redirect_stderr

                old_stdout = sys.stdout
                old_stderr = sys.stderr
                stdout_capture = io.StringIO()
                stderr_capture = io.StringIO()

                sys.stdout = stdout_capture
                sys.stderr = stderr_capture

                try:
                    self.connection_state = self.client_core.get_connection_state()

                    if command == 'dns-res':
                        self.client_core.command_handlers[command](args, self.connection_state)
                    else:
                        self.client_core.command_handlers[command](args)

                    output = stdout_capture.getvalue()
                    error = stderr_capture.getvalue()

                    if error:
                        output = f"{output}\n{error}"

                    output = output.strip()

                    with sqlite3.connect(self.client_core.db_path, timeout=10) as conn:
                        cursor = conn.cursor()
                        cursor.execute('INSERT INTO cli_history (command, timestamp, success, result) VALUES (?, ?, ?, ?)',
                                     (command_content, time.time(), 1, "Executed via controller"))
                        conn.commit()

                    return {'success': True, 'output': output}

                except Exception as e:
                    output = stdout_capture.getvalue()
                    error = stderr_capture.getvalue()

                    error_msg = f"{str(e)}\n{error}".strip()

                    with sqlite3.connect(self.client_core.db_path, timeout=10) as conn:
                        cursor = conn.cursor()
                        cursor.execute('INSERT INTO cli_history (command, timestamp, success, result) VALUES (?, ?, ?, ?)',
                                     (command_content, time.time(), 0, str(e)))
                        conn.commit()

                    return {'success': False, 'output': error_msg}

                finally:
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr

            else:
                return {'success': False, 'output': f"Unknown command: {command}"}

        except Exception as e:
            return {'success': False, 'output': str(e)}

    def send_command(self, command, args):
        try:
            command_content = f"{command} {' '.join(args)}".strip()

            self.write_controller_file(command_content)

            start_time = time.time()
            timeout = 300
            log_file = None

            while time.time() - start_time < timeout:
                if not os.path.exists(self.controller_file):
                    time.sleep(0.1)
                    continue

                content = self.read_controller_file()
                if content.startswith(self.logs_dir):
                    log_file = content
                    break
                time.sleep(0.1)

            if not log_file:
                return False, "Timeout waiting for log file creation"

            if not os.path.exists(log_file):
                return False, f"Log file not found: {log_file}"

            result = None
            start_wait = time.time()

            while time.time() - start_wait < timeout:
                log_data = self._read_log_file(log_file)

                if log_data:
                    status = log_data['status']
                    message = log_data['message']

                    if status == "1" and log_data.get('result'):
                        result = log_data['result']
                        if result == "1":
                            return True, message if message else "Command executed successfully"
                        else:
                            return False, message if message else "Command failed"

                    elif status == "0":
                        return False, message if message else "Command failed immediately"

                time.sleep(0.1)

            return False, "Timeout waiting for command execution"

        except Exception as e:
            return False, str(e)

class CLIDisplay:
    def __init__(self, no_cli=False):
        self.no_cli = no_cli
        try:
            self.console_width = shutil.get_terminal_size().columns
        except:
            self.console_width = 80
        self.colors = {
            'red': '\033[91m',
            'green': '\033[92m',
            'yellow': '\033[93m',
            'blue': '\033[94m',
            'magenta': '\033[95m',
            'cyan': '\033[96m',
            'white': '\033[97m',
            'reset': '\033[0m',
            'bold': '\033[1m',
            'dim': '\033[2m',
            'underline': '\033[4m',
            'blink': '\033[5m'
        }
        self.logo_colors = ['cyan', 'magenta', 'yellow', 'green', 'blue']
        self.color_index = 0

    def _next_color(self):
        color = self.logo_colors[self.color_index]
        self.color_index = (self.color_index + 1) % len(self.logo_colors)
        return self.colors[color]

    def print_header(self, text):
        if self.no_cli:
            print(f"\n{'='*80}\n{text}\n{'='*80}")
            return

        border = '╔' + '═' * (self.console_width - 2) + '╗'
        middle = '║' + text.center(self.console_width - 2) + '║'
        print(f"\n{self.colors['bold']}{self._next_color()}{border}{self.colors['reset']}")
        print(f"{self.colors['bold']}{self._next_color()}{middle}{self.colors['reset']}")
        print(f"{self.colors['bold']}{self._next_color()}{border.replace('╔', '╚').replace('╗', '╝')}{self.colors['reset']}\n")

    def print_section(self, text):
        if self.no_cli:
            print(f"\n{text}\n{'-'*len(text)}")
            return

        print(f"\n{self.colors['bold']}{self.colors['magenta']}▐ {text}{self.colors['reset']}")
        print(f"{self.colors['dim']}{'─' * (len(text) + 2)}{self.colors['reset']}")

    def print_success(self, text):
        if self.no_cli:
            print(f"[✓] {text}")
        else:
            print(f"{self.colors['green']}┃ ✓ {text}{self.colors['reset']}")

    def print_error(self, text):
        if self.no_cli:
            print(f"[✗] {text}")
        else:
            print(f"{self.colors['red']}┃ ✗ {text}{self.colors['reset']}")

    def print_warning(self, text):
        if self.no_cli:
            print(f"[!] {text}")
        else:
            print(f"{self.colors['yellow']}┃ ! {text}{self.colors['reset']}")

    def print_info(self, text):
        if self.no_cli:
            print(f"[i] {text}")
        else:
            print(f"{self.colors['blue']}┃ ℹ {text}{self.colors['reset']}")

    def print_progress(self, current, total, text="", bar_length=40):
        if self.no_cli:
            if current == total:
                print(f"[{text}] - 100%")
            return

        percent = float(current) / total
        arrow = '█' * int(round(percent * bar_length))
        spaces = '░' * (bar_length - len(arrow))

        bar_color = self.colors['green'] if percent > 0.7 else self.colors['yellow'] if percent > 0.3 else self.colors['red']

        sys.stdout.write(f"\r{bar_color}┃ [{arrow}{spaces}] {int(round(percent * 100))}% - {text}{self.colors['reset']}")
        sys.stdout.flush()

        if current == total:
            print()

    def print_table(self, headers, rows, max_width=80):
        if self.no_cli:
            print(" | ".join(headers))
            print("-+-".join(["-" * len(h) for h in headers]))
            for row in rows:
                print(" | ".join(str(cell) for cell in row))
            return

        col_widths = []
        for i, header in enumerate(headers):
            max_len = len(str(header))
            for row in rows:
                max_len = max(max_len, len(str(row[i])))
            col_widths.append(min(max_len, max_width // len(headers)))

        header_line = " │ ".join(f"{self.colors['bold']}{str(h).ljust(w)}{self.colors['reset']}"
                                for h, w in zip(headers, col_widths))
        separator = "─┼─".join("─" * w for w in col_widths)

        print(f"\n{header_line}")
        print(separator)

        for row_idx, row in enumerate(rows):
            row_line = " │ ".join(str(cell)[:w].ljust(w) for cell, w in zip(row, col_widths))
            if row_idx % 2 == 0:
                row_line = f"{self.colors['dim']}{row_line}{self.colors['reset']}"
            print(row_line)

    def print_key_value(self, key, value, indent=0):
        if self.no_cli:
            print(f"{' ' * indent}{key}: {value}")
        else:
            print(f"{' ' * indent}{self.colors['bold']}┃ {key}:{self.colors['reset']} {value}")

    def print_json(self, data, indent=2):
        formatted = json.dumps(data, indent=indent, ensure_ascii=False)
        if self.no_cli:
            print(formatted)
        else:
            formatted = re.sub(r'"(.*?)":', f'{self.colors["yellow"]}"\\1"{self.colors["reset"]}:', formatted)
            formatted = re.sub(r'(\d+)', f'{self.colors["cyan"]}\\1{self.colors["reset"]}', formatted)
            formatted = re.sub(r'(true|false|null)', f'{self.colors["magenta"]}\\1{self.colors["reset"]}', formatted)
            print(formatted)

    def clear_screen(self):
        if self.no_cli:
            return
        os.system('cls' if platform.system() == 'Windows' else 'clear')

    def get_input(self, prompt, password=False):
        if self.no_cli:
            if password:
                return getpass.getpass(prompt)
            return input(prompt)

        prompt_text = f"{self.colors['cyan']}┃ ➤ {prompt}{self.colors['reset']}"
        if password:
            return getpass.getpass(prompt_text)
        else:
            return input(prompt_text)

    def print_logo(self):
        if self.no_cli:
            return

        logo = f"""
{self.colors['bold']}{self.colors['cyan']}
╔══════════════════════════════════════════════════════════════════════════╗
║                                                                          ║
║        {self.colors['magenta']}╦ ╦ {self.colors['yellow']}╔═╗ {self.colors['green']}╔═╗      {self.colors['blue']}╔═╗╦  ╦{self.colors['cyan']}                                          ║
║        {self.colors['magenta']}╠═╣ {self.colors['yellow']}╠═╝ {self.colors['green']}╚═╗      {self.colors['blue']}║  ║  ║{self.colors['cyan']}                                          ║
║        {self.colors['magenta']}╩ ╩ {self.colors['yellow']}╩   {self.colors['green']}╚═╝      {self.colors['blue']}╚═╝╩═╝╩{self.colors['cyan']}                                          ║
║                                                                          ║
║                {self.colors['bold']}{self.colors['white']}HPS Command Line Interface{self.colors['cyan']}                                ║
║                {self.colors['dim']}{self.colors['white']}Decentralized P2P Network Client{self.colors['cyan']}                          ║
║                                                                          ║
╚══════════════════════════════════════════════════════════════════════════╝
{self.colors['reset']}"""

        print(logo)

    def print_status_bar(self, user=None, server=None, reputation=None):
        if self.no_cli:
            return

        status = []
        if user:
            status.append(f"{self.colors['green']}👤 {user}")
        if server:
            status.append(f"{self.colors['blue']}🌐 {server}")
        if reputation:
            status.append(f"{self.colors['yellow']}⭐ {reputation}")

        if status:
            status_text = f"{self.colors['dim']}┃{self.colors['reset']} " + f" {self.colors['dim']}│{self.colors['reset']} ".join(status)
            print(f"\n{status_text}")

class CLIPowSolver:
    def __init__(self, display):
        self.display = display
        self.is_solving = False
        self.solution_found = threading.Event()
        self.nonce_solution = None
        self.hashrate_observed = 0.0
        self.start_time = None
        self.total_hashes = 0

    def leading_zero_bits(self, h: bytes) -> int:
        count = 0
        for byte in h:
            if byte == 0:
                count += 8
            else:
                count += bin(byte)[2:].zfill(8).index('1')
                break
        return count

    def calibrate_hashrate(self, seconds: float = 1.0) -> float:
        message = secrets.token_bytes(16)
        end = time.time() + seconds
        count = 0
        nonce = 0

        while time.time() < end:
            data = message + struct.pack(">Q", nonce)
            _ = hashlib.sha256(data).digest()
            nonce += 1
            count += 1

        elapsed = seconds
        return count / elapsed if elapsed > 0 else 0.0

    def solve_challenge(self, challenge: str, target_bits: int, target_seconds: float, action_type: str = "login"):
        if self.is_solving:
            return

        self.is_solving = True
        self.solution_found.clear()
        self.nonce_solution = None
        self.start_time = time.time()
        self.total_hashes = 0

        self.display.print_section(f"Proof of Work - {action_type}")
        self.display.print_info(f"Target bits: {target_bits}")
        self.display.print_info(f"Target time: {target_seconds:.1f}s")

        def solve_thread():
            try:
                challenge_bytes = base64.b64decode(challenge)
                nonce = 0
                hash_count = 0
                last_update = self.start_time
                last_progress_update = self.start_time

                hashrate = self.calibrate_hashrate(0.5)
                self.hashrate_observed = hashrate

                self.display.print_info(f"Estimated hashrate: {hashrate:,.0f} H/s")
                self.display.print_info("Starting mining...")

                current_hashrate = 0.0
                attempts_per_second = 0
                stats_updates = 0

                while self.is_solving and time.time() - self.start_time < 600:
                    data = challenge_bytes + struct.pack(">Q", nonce)
                    hash_result = hashlib.sha256(data).digest()
                    hash_count += 1
                    attempts_per_second += 1
                    self.total_hashes += 1

                    lzb = self.leading_zero_bits(hash_result)

                    current_time = time.time()
                    elapsed = current_time - self.start_time

                    if current_time - last_update >= 1.0:
                        current_hashrate = attempts_per_second / (current_time - last_update)
                        last_update = current_time
                        attempts_per_second = 0

                        if current_time - last_progress_update >= 0.3:
                            progress_percent = min(int(elapsed/target_seconds*100), 99)
                            progress_text = f"Nonce: {nonce:,} | Time: {elapsed:.1f}s | Hashrate: {current_hashrate:,.0f} H/s | Hashes: {self.total_hashes:,}"
                            self.display.print_progress(progress_percent, 100, progress_text)
                            last_progress_update = current_time
                            stats_updates += 1

                    if lzb >= target_bits:
                        solve_time = current_time - self.start_time
                        self.nonce_solution = str(nonce)
                        self.hashrate_observed = current_hashrate

                        self.display.print_progress(100, 100, "Solution found!")
                        self.display.print_success(f"Solution found! Nonce: {nonce:,}")
                        self.display.print_info(f"Total time: {solve_time:.2f}s")
                        self.display.print_info(f"Final hashrate: {current_hashrate:,.0f} H/s")
                        self.display.print_info(f"Total hashes: {self.total_hashes:,}")

                        self.solution_found.set()
                        break

                    nonce += 1

                    if nonce % 10000 == 0:
                        time.sleep(0.001)

                    if nonce % 1000 == 0 and not self.is_solving:
                        break

                if not self.nonce_solution and self.is_solving:
                    self.display.print_error("Time limit exceeded")

            except Exception as e:
                logger.error(f"PoW mining error: {e}")
                self.display.print_error(f"Error: {e}")
            finally:
                self.is_solving = False

        threading.Thread(target=solve_thread, daemon=True).start()

    def wait_for_solution(self, timeout=600):
        return self.solution_found.wait(timeout)

    def stop_solving(self):
        self.is_solving = False

class HPSClientCore:
    def __init__(self, display=None, no_cli=False):
        self.display = display or CLIDisplay(no_cli)
        self.no_cli = no_cli

        self.current_user = None
        self.username = None
        self.password = None
        self.private_key = None
        self.public_key_pem = None
        self.session_id = str(uuid.uuid4())
        self.node_id = hashlib.sha256(self.session_id.encode()).hexdigest()[:32]
        self.connected = False
        self.peers = []
        self.content_cache = {}
        self.dns_cache = {}
        self.local_files = {}
        self.known_servers = []
        self.current_server = None
        self.server_nodes = []
        self.content_verification_cache = {}
        self.node_type = "client"
        self.connection_attempts = 0
        self.max_connection_attempts = 3
        self.reputation = 100
        self.rate_limits = {}
        self.banned_until = None
        self.client_identifier = self.generate_client_identifier()
        self.upload_blocked_until = 0
        self.login_blocked_until = 0
        self.dns_blocked_until = 0
        self.report_blocked_until = 0
        self.ban_duration = 0
        self.ban_reason = ""
        self.pow_solver = CLIPowSolver(self.display)
        self.max_upload_size = 100 * 1024 * 1024
        self.disk_quota = 500 * 1024 * 1024
        self.used_disk_space = 0
        self.private_key_passphrase = None
        self.server_public_keys = {}
        self.session_key = None
        self.server_auth_challenge = None
        self.client_auth_challenge = None
        self.ssl_verify = False
        self.use_ssl = False
        self.backup_server = None
        self.auto_reconnect = True
        self.via_controller = False

        self.stats_data = {
            'session_start': 0,
            'data_sent': 0,
            'data_received': 0,
            'content_downloaded': 0,
            'content_uploaded': 0,
            'dns_registered': 0,
            'pow_solved': 0,
            'pow_time': 0,
            'content_reported': 0,
            'hashes_calculated': 0
        }

        self.loop = None
        self.sio = None
        self.network_thread = None
        self.reconnect_thread = None
        self.is_running = True
        self.reconnect_lock = threading.Lock()
        self.connection_ready = threading.Event()

        self.crypto_dir = os.path.join(os.path.expanduser("~"), ".hps_cli")
        os.makedirs(self.crypto_dir, exist_ok=True)
        self.db_path = os.path.join(self.crypto_dir, "hps_cli.db")

        self.init_database()
        self.load_known_servers()
        self.load_session_state()
        self.setup_cryptography()

        self.start_network_thread()

        self.calculate_disk_usage()

        self.command_handlers = {}
        self.setup_command_handlers()

        self.auth_event = threading.Event()
        self.auth_result = None
        self.upload_event = threading.Event()
        self.upload_result = None
        self.dns_event = threading.Event()
        self.dns_result = None
        self.report_event = threading.Event()
        self.report_result = None
        self.content_event = threading.Event()
        self.content_result = None
        self.search_event = threading.Event()
        self.search_result = None
        self.network_event = threading.Event()
        self.network_result = None

    def init_database(self):
        with sqlite3.connect(self.db_path, timeout=30) as conn:
            cursor = conn.cursor()

            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA synchronous=NORMAL')
            cursor.execute('PRAGMA foreign_keys=ON')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_network_nodes (
node_id TEXT PRIMARY KEY,
address TEXT NOT NULL,
node_type TEXT NOT NULL,
reputation INTEGER DEFAULT 100,
status TEXT NOT NULL,
last_seen REAL NOT NULL
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_dns_records (
domain TEXT PRIMARY KEY,
content_hash TEXT NOT NULL,
username TEXT NOT NULL,
verified INTEGER DEFAULT 0,
timestamp REAL NOT NULL,
ddns_hash TEXT NOT NULL DEFAULT ''
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_known_servers (
server_address TEXT PRIMARY KEY,
reputation INTEGER DEFAULT 100,
last_connected REAL NOT NULL,
is_active INTEGER DEFAULT 1,
use_ssl INTEGER DEFAULT 0
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_content_cache (
content_hash TEXT PRIMARY KEY,
file_path TEXT NOT NULL,
file_name TEXT NOT NULL,
mime_type TEXT NOT NULL,
size INTEGER NOT NULL,
last_accessed REAL NOT NULL,
title TEXT,
description TEXT,
username TEXT,
signature TEXT,
public_key TEXT,
verified INTEGER DEFAULT 0
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_ddns_cache (
domain TEXT PRIMARY KEY,
ddns_hash TEXT NOT NULL,
content_hash TEXT NOT NULL,
username TEXT NOT NULL,
verified INTEGER DEFAULT 0,
timestamp REAL NOT NULL
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_settings (
key TEXT PRIMARY KEY,
value TEXT NOT NULL
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_reports (
report_id TEXT PRIMARY KEY,
content_hash TEXT NOT NULL,
reported_user TEXT NOT NULL,
reporter_user TEXT NOT NULL,
timestamp REAL NOT NULL,
status TEXT NOT NULL,
reason TEXT
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_history (
id INTEGER PRIMARY KEY AUTOINCREMENT,
command TEXT NOT NULL,
timestamp REAL NOT NULL,
success INTEGER DEFAULT 0,
result TEXT
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_session (
key TEXT PRIMARY KEY,
value TEXT NOT NULL,
updated REAL NOT NULL
)
            ''')

            cursor.execute('''
CREATE TABLE IF NOT EXISTS cli_stats (
stat_key TEXT PRIMARY KEY,
stat_value INTEGER NOT NULL,
updated REAL NOT NULL
)
            ''')

            conn.commit()

    def load_known_servers(self):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT server_address, use_ssl FROM cli_known_servers WHERE is_active = 1')
            self.known_servers = []
            for row in cursor.fetchall():
                self.known_servers.append(row[0])
                if row[1]:
                    self.use_ssl = True

    def save_known_servers(self):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            for server_address in self.known_servers:
                use_ssl = 1 if server_address.startswith('https://') else 0
                cursor.execute(
                    '''INSERT OR REPLACE INTO cli_known_servers
(server_address, last_connected, is_active, use_ssl)
                    VALUES (?, ?, ?, ?)''',
                    (server_address, time.time(), 1, use_ssl)
                )
            conn.commit()

    def load_session_state(self):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM cli_session')
            for key, value in cursor.fetchall():
                if key == 'current_user':
                    self.current_user = value
                elif key == 'current_server':
                    self.current_server = value
                elif key == 'reputation':
                    self.reputation = int(value)
                elif key == 'username':
                    self.username = value

            cursor.execute('SELECT stat_key, stat_value FROM cli_stats')
            for stat_key, stat_value in cursor.fetchall():
                if stat_key in self.stats_data:
                    self.stats_data[stat_key] = int(stat_value)

    def save_session_state(self):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            session_data = [
                ('current_user', str(self.current_user or ''), time.time()),
                ('current_server', str(self.current_server or ''), time.time()),
                ('reputation', str(self.reputation), time.time()),
                ('username', str(self.username or ''), time.time())
            ]

            for key, value, updated in session_data:
                cursor.execute('INSERT OR REPLACE INTO cli_session (key, value, updated) VALUES (?, ?, ?)',
                             (key, value, updated))

            for stat_key, stat_value in self.stats_data.items():
                cursor.execute('INSERT OR REPLACE INTO cli_stats (stat_key, stat_value, updated) VALUES (?, ?, ?)',
                             (stat_key, stat_value, time.time()))

            conn.commit()

    def calculate_disk_usage(self):
        if os.path.exists(self.crypto_dir):
            total_size = 0
            for dirpath, dirnames, filenames in os.walk(self.crypto_dir):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if os.path.exists(fp):
                        total_size += os.path.getsize(fp)
            self.used_disk_space = total_size

    def generate_client_identifier(self):
        machine_id = hashlib.sha256(str(uuid.getnode()).encode()).hexdigest()
        return hashlib.sha256((machine_id + self.session_id).encode()).hexdigest()

    def setup_cryptography(self):
        private_key_path = os.path.join(self.crypto_dir, "private_key.pem")
        public_key_path = os.path.join(self.crypto_dir, "public_key.pem")

        if os.path.exists(private_key_path) and os.path.exists(public_key_path):
            try:
                with open(private_key_path, "rb") as f:
                    self.private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
                with open(public_key_path, "rb") as f:
                    self.public_key_pem = f.read()
                if not self.no_cli:
                    self.display.print_info("Cryptographic keys loaded from local storage.")
            except Exception as e:
                self.display.print_error(f"Error loading existing keys: {e}")
                self.generate_keys()
        else:
            self.generate_keys()

    def generate_keys(self):
        try:
            self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096, backend=default_backend())
            self.public_key_pem = self.private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )
            if not self.no_cli:
                self.display.print_info("New cryptographic keys generated.")
            self.save_keys()
        except Exception as e:
            self.display.print_error(f"Error generating keys: {e}")

    def save_keys(self):
        try:
            private_key_path = os.path.join(self.crypto_dir, "private_key.pem")
            public_key_path = os.path.join(self.crypto_dir, "public_key.pem")

            with open(private_key_path, "wb") as f:
                f.write(self.private_key.private_bytes(
                            encoding=serialization.Encoding.PEM,
                            format=serialization.PrivateFormat.PKCS8,
                            encryption_algorithm=serialization.NoEncryption()
                        ))

            with open(public_key_path, "wb") as f:
                f.write(self.public_key_pem)

            if not self.no_cli:
                self.display.print_info("Cryptographic keys saved locally.")
        except Exception as e:
            self.display.print_error(f"Error saving keys: {e}")

    def start_network_thread(self):
        def run_network():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            ssl_context = None
            if self.use_ssl:
                ssl_context = ssl.create_default_context()
                if not self.ssl_verify:
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE

            self.sio = socketio.AsyncClient(ssl_verify=ssl_context if ssl_context else False,
                                          reconnection=True,
                                          reconnection_attempts=5,
                                          reconnection_delay=1,
                                          reconnection_delay_max=5)
            self.setup_socket_handlers()

            self.connection_ready.set()
            self.loop.run_forever()

        self.network_thread = threading.Thread(target=run_network, daemon=True)
        self.network_thread.start()
        self.connection_ready.wait(timeout=10)

    def setup_socket_handlers(self):
        @self.sio.event
        async def connect():
            self.connected = True
            self.display.print_success(f"Connected to server {self.current_server}")
            self.connection_attempts = 0
            await self.sio.emit('request_server_auth_challenge', {})

        @self.sio.event
        async def disconnect():
            self.connected = False
            self.display.print_warning(f"Disconnected from server {self.current_server}")
            if self.auto_reconnect and self.is_running:
                self.start_reconnect_thread()

        @self.sio.event
        async def connect_error(data):
            self.display.print_error(f"Connection error: {data}")

        @self.sio.event
        async def server_auth_challenge(data):
            challenge = data.get('challenge')
            server_public_key_b64 = data.get('server_public_key')
            server_signature_b64 = data.get('signature')

            if not all([challenge, server_public_key_b64, server_signature_b64]):
                self.display.print_error("Server authentication challenge incomplete")
                return

            try:
                server_public_key = serialization.load_pem_public_key(base64.b64decode(server_public_key_b64), backend=default_backend())
                server_public_key.verify(
                    base64.b64decode(server_signature_b64),
                    challenge.encode('utf-8'),
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                    hashes.SHA256()
                )

                self.server_public_keys[self.current_server] = server_public_key_b64

                client_challenge = secrets.token_urlsafe(32)
                self.client_auth_challenge = client_challenge

                client_signature = self.private_key.sign(
                    client_challenge.encode('utf-8'),
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                    hashes.SHA256()
                )

                await self.sio.emit('verify_server_auth_response', {
                    'client_challenge': client_challenge,
                    'client_signature': base64.b64encode(client_signature).decode('utf-8'),
                    'client_public_key': base64.b64encode(self.public_key_pem).decode('utf-8')
                })

            except InvalidSignature:
                self.display.print_error("Invalid server signature")
            except Exception as e:
                self.display.print_error(f"Server authentication error: {str(e)}")

        @self.sio.event
        async def server_auth_result(data):
            success = data.get('success', False)
            if success:
                self.display.print_info("Server authenticated successfully")
                if hasattr(self, 'pending_login'):
                    await self.request_pow_challenge("login")
            else:
                error = data.get('error', 'Unknown error')
                self.display.print_error(f"Server authentication failed: {error}")

        @self.sio.event
        async def pow_challenge(data):
            if 'error' in data:
                error = data['error']
                self.display.print_error(f"PoW challenge error: {error}")
                if 'blocked_until' in data:
                    blocked_until = data['blocked_until']
                    duration = blocked_until - time.time()
                    self.handle_ban(duration, "Rate limit exceeded")
                return

            challenge = data.get('challenge')
            target_bits = data.get('target_bits')
            target_seconds = data.get('target_seconds', 30.0)
            action_type = data.get('action_type', 'login')

            self.display.print_info(f"PoW challenge received: {target_bits} bits")

            self.pow_solver.solve_challenge(challenge, target_bits, target_seconds, action_type)

            if self.pow_solver.wait_for_solution():
                nonce = self.pow_solver.nonce_solution
                hashrate = self.pow_solver.hashrate_observed
                pow_time = time.time() - self.pow_solver.start_time

                self.stats_data['pow_solved'] += 1
                self.stats_data['pow_time'] += pow_time
                self.stats_data['hashes_calculated'] += self.pow_solver.total_hashes

                if action_type == "login":
                    await self.send_authentication(nonce, hashrate)
                elif action_type == "upload":
                    if hasattr(self, 'pending_upload'):
                        await self._upload_file(*self.pending_upload, nonce, hashrate)
                elif action_type == "dns":
                    if hasattr(self, 'pending_dns'):
                        await self._register_dns(*self.pending_dns, nonce, hashrate)
                elif action_type == "report":
                    if hasattr(self, 'pending_report'):
                        await self._report_content(*self.pending_report, nonce, hashrate)
            else:
                self.display.print_error("PoW solution failed")

        @self.sio.event
        async def authentication_result(data):
            success = data.get('success', False)
            if success:
                username = data.get('username')
                reputation = data.get('reputation', 100)
                self.current_user = username
                self.username = username
                self.reputation = reputation
                self.stats_data['session_start'] = time.time()

                if self.via_controller:
                    print("Login successful")
                else:
                    self.display.print_success(f"Login successful: {username}")
                    self.display.print_info(f"Reputation: {reputation}")

                if self.current_server and self.current_server not in self.known_servers:
                    self.known_servers.append(self.current_server)
                    self.save_known_servers()

                self.auth_result = data
                self.auth_event.set()

                await self.join_network()
                await self.sync_client_files()
                self.save_session_state()
                if hasattr(self, 'pending_login'):
                    del self.pending_login
            else:
                error = data.get('error', 'Unknown error')
                if self.via_controller:
                    print(f"Login failed: {error}")
                else:
                    self.display.print_error(f"Login failed: {error}")
                self.auth_result = data
                self.auth_event.set()
                if hasattr(self, 'pending_login'):
                    del self.pending_login

        @self.sio.event
        async def content_response(data):
            if 'error' in data:
                error = data['error']
                if self.via_controller:
                    print(f"Content error: {error}")
                else:
                    self.display.print_error(f"Content error: {error}")
                self.content_result = {'error': error}
                self.content_event.set()
                return

            content_b64 = data.get('content')
            title = data.get('title', 'No title')
            description = data.get('description', '')
            mime_type = data.get('mime_type', 'text/plain')
            username = data.get('username', 'Unknown')
            signature = data.get('signature', '')
            public_key = data.get('public_key', '')
            verified = data.get('verified', False)
            content_hash = data.get('content_hash', '')

            try:
                content = base64.b64decode(content_b64)
                self.stats_data['data_received'] += len(content)
                self.stats_data['content_downloaded'] += 1

                integrity_ok = True
                actual_hash = hashlib.sha256(content).hexdigest()
                if actual_hash != content_hash:
                    integrity_ok = False
                    if not self.via_controller:
                        self.display.print_warning("File integrity compromised!")

                self.save_content_to_storage(content_hash, content, {
                    'title': title,
                    'description': description,
                    'mime_type': mime_type,
                    'username': username,
                    'signature': signature,
                    'public_key': public_key,
                    'verified': verified
                })

                content_info = {
                    'title': title,
                    'description': description,
                    'mime_type': mime_type,
                    'username': username,
                    'signature': signature,
                    'public_key': public_key,
                    'verified': verified,
                    'content': content,
                    'content_hash': content_hash,
                    'reputation': data.get('reputation', 100),
                    'integrity_ok': integrity_ok,
                }

                self.content_result = content_info
                self.content_event.set()
                self.save_session_state()

            except Exception as e:
                if self.via_controller:
                    print(f"Error decoding content: {e}")
                else:
                    self.display.print_error(f"Error decoding content: {e}")
                self.content_result = {'error': str(e)}
                self.content_event.set()

        @self.sio.event
        async def publish_result(data):
            success = data.get('success', False)
            if success:
                content_hash = data.get('content_hash')
                self.stats_data['content_uploaded'] += 1
                if self.via_controller:
                    print(f"Upload successful! Hash: {content_hash}")
                else:
                    self.display.print_success(f"Upload successful! Hash: {content_hash}")
                self.upload_result = data
                self.upload_event.set()
                self.save_session_state()
            else:
                error = data.get('error', 'Unknown error')
                if self.via_controller:
                    print(f"Upload failed: {error}")
                else:
                    self.display.print_error(f"Upload failed: {error}")
                self.upload_result = data
                self.upload_event.set()
            if hasattr(self, 'pending_upload'):
                del self.pending_upload

        @self.sio.event
        async def dns_result(data):
            success = data.get('success', False)
            if success:
                domain = data.get('domain')
                self.stats_data['dns_registered'] += 1
                if self.via_controller:
                    print(f"DNS registered: {domain}")
                else:
                    self.display.print_success(f"DNS registered: {domain}")
                self.dns_result = data
                self.dns_event.set()
                self.save_session_state()
            else:
                error = data.get('error', 'Unknown error')
                if self.via_controller:
                    print(f"DNS registration failed: {error}")
                else:
                    self.display.print_error(f"DNS registration failed: {error}")
                self.dns_result = data
                self.dns_event.set()
            if hasattr(self, 'pending_dns'):
                del self.pending_dns

        @self.sio.event
        async def dns_resolution(data):
            success = data.get('success', False)
            if success:
                domain = data.get('domain')
                content_hash = data.get('content_hash')
                username = data.get('username')
                verified = data.get('verified', False)

                if self.via_controller:
                    print(content_hash)
                else:
                    self.display.print_success(f"DNS resolved: {domain} -> {content_hash}")
                    self.display.print_info(f"Owner: {username}")
                    self.display.print_info(f"Verified: {'Yes' if verified else 'No'}")

                with sqlite3.connect(self.db_path, timeout=10) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
INSERT OR REPLACE INTO cli_dns_records
(domain, content_hash, username, verified, timestamp, ddns_hash)
VALUES (?, ?, ?, ?, ?, ?)
                        ''', (domain, content_hash, username, verified, time.time(), ""))
                    conn.commit()

                self.dns_result = data
                self.dns_event.set()
            else:
                error = data.get('error', 'Unknown error')
                if self.via_controller:
                    print(f"DNS resolution failed: {error}")
                else:
                    self.display.print_error(f"DNS resolution failed: {error}")
                self.dns_result = data
                self.dns_event.set()

        @self.sio.event
        async def search_results(data):
            if 'error' in data:
                error = data['error']
                if self.via_controller:
                    print(f"Search error: {error}")
                else:
                    self.display.print_error(f"Search error: {error}")
                self.search_result = {'error': error}
                self.search_event.set()
                return

            results = data.get('results', [])
            if self.via_controller:
                for result in results:
                    print(f"{result.get('content_hash')}|{result.get('title')}|{result.get('username')}")
            self.search_result = results
            self.search_event.set()

        @self.sio.event
        async def network_state(data):
            if 'error' in data:
                self.network_result = {'error': data['error']}
                self.network_event.set()
                return

            self.network_result = data
            self.network_event.set()

        @self.sio.event
        async def report_result(data):
            success = data.get('success', False)
            if success:
                self.stats_data['content_reported'] += 1
                if self.via_controller:
                    print("Content reported successfully!")
                else:
                    self.display.print_success("Content reported successfully!")
                self.report_result = data
                self.report_event.set()
                self.save_session_state()
            else:
                error = data.get('error', 'Unknown error')
                if self.via_controller:
                    print(f"Report failed: {error}")
                else:
                    self.display.print_error(f"Report failed: {error}")
                self.report_result = data
                self.report_event.set()
            if hasattr(self, 'pending_report'):
                del self.pending_report

    def start_reconnect_thread(self):
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            return

        def reconnect():
            time.sleep(2)
            if not self.is_running:
                return
            with self.reconnect_lock:
                if not self.connected and self.current_server and self.is_running:
                    self.display.print_info(f"Attempting to reconnect to {self.current_server}...")
                    asyncio.run_coroutine_threadsafe(self._reconnect(), self.loop)

        self.reconnect_thread = threading.Thread(target=reconnect, daemon=True)
        self.reconnect_thread.start()

    async def _reconnect(self):
        try:
            if self.sio and not self.sio.connected:
                await self.sio.connect(self.current_server, wait_timeout=10)
        except Exception as e:
            self.display.print_error(f"Reconnection failed: {e}")

    async def request_pow_challenge(self, action_type):
        if not self.connected:
            self.display.print_error("Not connected to server")
            return

        await self.sio.emit('request_pow_challenge', {
            'client_identifier': self.client_identifier,
            'action_type': action_type
        })

    async def send_authentication(self, pow_nonce, hashrate_observed):
        if not self.connected:
            return

        password_hash = hashlib.sha256(self.password.encode()).hexdigest()

        if not self.client_auth_challenge:
            self.display.print_error("Client authentication challenge missing")
            return

        client_challenge_signature = self.private_key.sign(
            self.client_auth_challenge.encode('utf-8'),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )

        await self.sio.emit('authenticate', {
            'username': self.username,
            'password_hash': password_hash,
            'public_key': base64.b64encode(self.public_key_pem).decode('utf-8'),
            'node_type': 'client',
            'client_identifier': self.client_identifier,
            'pow_nonce': pow_nonce,
            'hashrate_observed': hashrate_observed,
            'client_challenge_signature': base64.b64encode(client_challenge_signature).decode('utf-8'),
            'client_challenge': self.client_auth_challenge
        })

    async def join_network(self):
        if not self.connected or not self.current_user:
            return

        await self.sio.emit('join_network', {
            'node_id': self.node_id,
            'address': f"client_{self.client_identifier}",
            'public_key': base64.b64encode(self.public_key_pem).decode('utf-8'),
            'username': self.current_user,
            'node_type': 'client',
            'client_identifier': self.client_identifier
        })

    async def sync_client_files(self):
        if not self.connected or not self.current_user:
            return

        files = []
        content_dir = os.path.join(self.crypto_dir, "content")
        if os.path.exists(content_dir):
            for filename in os.listdir(content_dir):
                if filename.endswith('.dat'):
                    file_path = os.path.join(content_dir, filename)
                    content_hash = filename[:-4]
                    file_size = os.path.getsize(file_path)
                    files.append({
                        'content_hash': content_hash,
                        'file_name': filename,
                        'file_size': file_size
                    })

        await self.sio.emit('sync_client_files', {
            'files': files
        })

    def handle_ban(self, duration, reason):
        self.banned_until = time.time() + duration
        self.ban_duration = duration
        self.ban_reason = reason
        self.display.print_warning(f"Banned for {int(duration)}s: {reason}")

    def save_content_to_storage(self, content_hash, content, metadata=None):
        content_dir = os.path.join(self.crypto_dir, "content")
        os.makedirs(content_dir, exist_ok=True)

        file_path = os.path.join(content_dir, f"{content_hash}.dat")
        with open(file_path, 'wb') as f:
            f.write(content)

        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            if metadata:
                cursor.execute('''
INSERT OR REPLACE INTO cli_content_cache
(content_hash, file_path, file_name, mime_type, size, last_accessed, title, description, username, signature, public_key, verified)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        content_hash, file_path, f"{content_hash}.dat",
                        metadata.get('mime_type', 'application/octet-stream'),
                        len(content), time.time(),
                        metadata.get('title', ''),
                        metadata.get('description', ''),
                        metadata.get('username', ''),
                        metadata.get('signature', ''),
                        metadata.get('public_key', ''),
                        metadata.get('verified', 0)
                    ))
            else:
                cursor.execute('''
INSERT OR REPLACE INTO cli_content_cache
(content_hash, file_path, file_name, mime_type, size, last_accessed)
VALUES (?, ?, ?, ?, ?, ?)
                    ''', (content_hash, file_path, f"{content_hash}.dat", 'application/octet-stream', len(content), time.time()))
            conn.commit()

        self.calculate_disk_usage()

    async def _connect_to_server(self, server_address):
        try:
            if self.sio and self.sio.connected:
                await self.sio.disconnect()

            protocol = "https" if server_address.startswith('https://') else "http"
            if not server_address.startswith(('http://', 'https://')):
                server_address = f"{protocol}://{server_address}"

            self.display.print_info(f"Connecting to {server_address}...")

            await self.sio.connect(server_address, wait_timeout=10)
            return True

        except Exception as e:
            self.display.print_error(f"Connection error: {e}")
            return False

    async def _connect_and_login(self, args):
        if len(args) < 3:
            return False

        server, username, password = args[0], args[1], args[2]

        self.current_server = server
        self.username = username
        self.password = password
        self.pending_login = True

        self.display.print_info(f"Connecting to {server}...")

        try:
            result = await self._connect_to_server(server)
            if not result:
                return False

            self.auth_event.clear()
            self.auth_result = None

            start_time = time.time()
            while time.time() - start_time < 30:
                if self.auth_event.is_set():
                    break
                await asyncio.sleep(0.1)

            if self.auth_event.is_set():
                return self.auth_result and self.auth_result.get('success')
            else:
                return False

        except Exception as e:
            self.display.print_error(f"Connection error: {e}")
            return False

    def setup_command_handlers(self):
        self.command_handlers = {
            'login': self.handle_login,
            'logout': self.handle_logout,
            'upload': self.handle_upload,
            'download': self.handle_download,
            'dns-reg': self.handle_dns_register,
            'dns-res': self.handle_dns_resolve,
            'search': self.handle_search,
            'network': self.handle_network,
            'stats': self.handle_stats,
            'report': self.handle_report,
            'security': self.handle_security,
            'servers': self.handle_servers,
            'keys': self.handle_keys,
            'sync': self.handle_sync,
            'history': self.handle_history,
            'clear': self.handle_clear,
            'help': self.handle_help,
            'exit': self.handle_exit,
            'quit': self.handle_exit,
        }

    def handle_login(self, args):
        if len(args) < 3:
            if not self.no_cli:
                server = self.display.get_input("Server (ex: localhost:8080): ")
                username = self.display.get_input("Username: ")
                password = self.display.get_input("Password: ", password=True)
            else:
                self.display.print_error("Usage: login <server> <username> <password>")
                return
        else:
            server, username, password = args[0], args[1], args[2]

        self.current_server = server
        self.username = username
        self.password = password
        self.pending_login = True

        self.display.print_info(f"Connecting to {server}...")

        try:
            future = asyncio.run_coroutine_threadsafe(self._connect_and_login([server, username, password]), self.loop)
            result = future.result(30)
        except Exception as e:
            self.display.print_error(f"Connection failed: {e}")
            result = None

        if result:
            if self.via_controller:
                print("Login successful")
            else:
                self.display.print_success("Login successful!")
        else:
            if self.via_controller:
                print("Login failed")
            else:
                self.display.print_error("Login failed")

    def handle_logout(self, args):
        if not self.current_user:
            self.display.print_warning("You are not logged in")
            return

        self.current_user = None
        self.username = None
        self.connected = False
        if self.sio and self.sio.connected:
            self.run_async(self.sio.disconnect())
        if self.via_controller:
            print("Logout successful")
        else:
            self.display.print_success("Logout successful")
        self.save_session_state()

    def handle_upload(self, args):
        if not self.current_user:
            self.display.print_error("You need to be logged in to upload")
            return

        if len(args) < 1:
            if not self.no_cli:
                file_path = self.display.get_input("File path: ")
                title = self.display.get_input("Title (Enter for filename): ")
                description = self.display.get_input("Description (optional): ")
                mime_type = self.display.get_input("MIME type (Enter for auto-detect): ")
            else:
                self.display.print_error("Usage: upload <file_path> [--title TITLE] [--desc DESCRIPTION] [--mime MIME_TYPE]")
                return
        else:
            file_path = args[0]
            title = None
            description = ""
            mime_type = None

            i = 1
            while i < len(args):
                if args[i] == '--title' and i+1 < len(args):
                    title = args[i+1]
                    i += 2
                elif args[i] == '--desc' and i+1 < len(args):
                    description = args[i+1]
                    i += 2
                elif args[i] == '--mime' and i+1 < len(args):
                    mime_type = args[i+1]
                    i += 2
                else:
                    self.display.print_error(f"Unknown argument: {args[i]}")
                    return

        if not os.path.exists(file_path):
            self.display.print_error(f"File not found: {file_path}")
            return

        if title is None:
            title = os.path.basename(file_path)

        if mime_type is None:
            mime_type, _ = mimetypes.guess_type(file_path)
            if not mime_type:
                mime_type = 'application/octet-stream'

        if not self.via_controller:
            self.display.print_section("File Upload")
            self.display.print_info(f"File: {file_path}")
            self.display.print_info(f"Title: {title}")
            self.display.print_info(f"MIME type: {mime_type}")

        try:
            with open(file_path, 'rb') as f:
                content = f.read()

            if len(content) > self.max_upload_size:
                self.display.print_error(f"File too large. Max size: {self.max_upload_size // (1024*1024)}MB")
                return

            header = b"# HSYST P2P SERVICE"
            header += b"### START:"
            header += b"# USER: " + self.current_user.encode('utf-8') + b""
            header += b"# KEY: " + base64.b64encode(self.public_key_pem) + b""
            header += b"### :END START"

            full_content_with_header = header + content
            content_hash = hashlib.sha256(full_content_with_header).hexdigest()

            signature = self.private_key.sign(
                content,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256()
            )

            self.save_content_to_storage(content_hash, full_content_with_header, {
                'title': title,
                'description': description,
                'mime_type': mime_type,
                'username': self.current_user,
                'signature': base64.b64encode(signature).decode('utf-8'),
                'public_key': base64.b64encode(self.public_key_pem).decode('utf-8'),
                'verified': True
            })

            self.pending_upload = (content_hash, title, description, mime_type, len(full_content_with_header), signature, full_content_with_header)
            self.upload_event.clear()
            self.upload_result = None

            self.run_async(self.request_pow_challenge("upload"))

            if not self.upload_event.wait(300):
                self.display.print_error("Upload timeout")
                if hasattr(self, 'pending_upload'):
                    del self.pending_upload
                return

            if self.upload_result and self.upload_result.get('success'):
                hash_value = self.upload_result.get('content_hash', '')
                if self.via_controller:
                    print(f"Upload completed. Hash: {hash_value}")
                else:
                    self.display.print_success(f"Upload completed successfully!")
                    self.display.print_info(f"Hash: {hash_value}")
            else:
                if self.via_controller:
                    print("Upload failed")
                else:
                    self.display.print_error("Upload failed")

        except Exception as e:
            if self.via_controller:
                print(f"Upload error: {e}")
            else:
                self.display.print_error(f"Upload error: {e}")

    async def _upload_file(self, content_hash, title, description, mime_type, size, signature, full_content_with_header, pow_nonce, hashrate_observed):
        if not self.connected:
            return

        try:
            content_b64 = base64.b64encode(full_content_with_header).decode('utf-8')
            data = {
                'content_hash': content_hash,
                'title': title,
                'description': description,
                'mime_type': mime_type,
                'size': size,
                'signature': base64.b64encode(signature).decode('utf-8'),
                'public_key': base64.b64encode(self.public_key_pem).decode('utf-8'),
                'content_b64': content_b64,
                'pow_nonce': pow_nonce,
                'hashrate_observed': hashrate_observed
            }

            await self.sio.emit('publish_content', data)

        except Exception as e:
            self.display.print_error(f"Upload error: {e}")

    def handle_download(self, args):
        if not self.current_user:
            self.display.print_error("You need to be logged in to download")
            return

        if len(args) < 1:
            self.display.print_error("Usage: download <hash_or_url> [--output PATH]")
            return

        target = args[0]
        output_path = None

        i = 1
        while i < len(args):
            if args[i] == '--output' and i+1 < len(args):
                output_path = args[i+1]
                i += 2
            else:
                self.display.print_error(f"Unknown argument: {args[i]}")
                return

        if not self.via_controller:
            self.display.print_section("Content Download")

        if target.startswith('hps://'):
            if target == 'hps://rede':
                self.display.print_info("Showing P2P network...")
                self.handle_network([])
                return
            elif target.startswith('hps://dns:'):
                domain = target[len('hps://dns:'):]
                self.display.print_info(f"Resolving DNS: {domain}")
                self.handle_dns_resolve([domain])
                return
            else:
                content_hash = target[len('hps://'):]
        else:
            content_hash = target

        self.content_event.clear()
        self.content_result = None

        self.run_async(self._request_content_by_hash(content_hash))

        if not self.content_event.wait(30):
            self.display.print_error("Download timeout")
            return

        if self.content_result and 'error' not in self.content_result:
            content_info = self.content_result

            if output_path is None:
                output_path = f"./{content_info['title']}"
                if not os.path.splitext(output_path)[1]:
                    ext = mimetypes.guess_extension(content_info['mime_type']) or '.dat'
                    output_path += ext

            try:
                with open(output_path, 'wb') as f:
                    f.write(content_info['content'])

                if self.via_controller:
                    print(output_path)
                else:
                    self.display.print_success(f"Content saved to: {output_path}")
                    self.display.print_info(f"Title: {content_info['title']}")
                    self.display.print_info(f"Author: {content_info['username']}")
                    self.display.print_info(f"Type: {content_info['mime_type']}")
                    self.display.print_info(f"Size: {len(content_info['content'])} bytes")
                    self.display.print_info(f"Verified: {'Yes' if content_info['verified'] else 'No'}")
            except Exception as e:
                if self.via_controller:
                    print(f"Error saving file: {e}")
                else:
                    self.display.print_error(f"Error saving file: {e}")
        else:
            if self.via_controller:
                print("Download failed")
            else:
                self.display.print_error("Download failed")

    async def _request_content_by_hash(self, content_hash):
        if not self.connected:
            return

        await self.sio.emit('request_content', {'content_hash': content_hash})

    def handle_dns_register(self, args):
        if not self.current_user:
            self.display.print_error("You need to be logged in to register DNS")
            return

        if len(args) < 2:
            self.display.print_error("Usage: dns-reg <domain> <content_hash>")
            return

        domain = args[0].lower()
        content_hash = args[1]

        if not self.is_valid_domain(domain):
            self.display.print_error("Invalid domain. Use only letters, numbers and hyphens.")
            return

        if not self.via_controller:
            self.display.print_section("DNS Registration")
            self.display.print_info(f"Domain: {domain}")
            self.display.print_info(f"Hash: {content_hash}")

        ddns_content = self.create_ddns_file(domain, content_hash)
        ddns_hash = hashlib.sha256(ddns_content).hexdigest()

        signature = self.private_key.sign(
            ddns_content,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )

        self.pending_dns = (domain, ddns_content, signature)
        self.dns_event.clear()
        self.dns_result = None

        self.run_async(self.request_pow_challenge("dns"))

        if not self.dns_event.wait(300):
            self.display.print_error("DNS registration timeout")
            if hasattr(self, 'pending_dns'):
                del self.pending_dns
            return

        if self.dns_result and self.dns_result.get('success'):
            if self.via_controller:
                print("DNS registered successfully")
            else:
                self.display.print_success("DNS registered successfully!")
        else:
            if self.via_controller:
                print("DNS registration failed")
            else:
                self.display.print_error("DNS registration failed")

    async def _register_dns(self, domain, ddns_content, signature, pow_nonce, hashrate_observed):
        if not self.connected:
            return

        try:
            ddns_content_b64 = base64.b64encode(ddns_content).decode('utf-8')
            await self.sio.emit('register_dns', {
                'domain': domain,
                'ddns_content': ddns_content_b64,
                'signature': base64.b64encode(signature).decode('utf-8'),
                'public_key': base64.b64encode(self.public_key_pem).decode('utf-8'),
                'pow_nonce': pow_nonce,
                'hashrate_observed': hashrate_observed
            })
        except Exception as e:
            self.display.print_error(f"DNS registration error: {e}")

    def create_ddns_file(self, domain, content_hash):
        ddns_content = f"""# HSYST P2P SERVICE
### START:
            # USER: {self.current_user}
            # KEY: {base64.b64encode(self.public_key_pem).decode('utf-8')}
### :END START
### DNS:
            # DNAME: {domain} = {content_hash}
### :END DNS
            """
        return ddns_content.encode('utf-8')

    def handle_dns_resolve(self, args, connection_state=None):
        if not self.current_user and not connection_state:
            self.display.print_error("You need to be logged in to resolve DNS")
            return

        if len(args) < 1:
            self.display.print_error("Usage: dns-res <domain>")
            return

        domain = args[0].lower()

        if not self.via_controller:
            self.display.print_section("DNS Resolution")
            self.display.print_info(f"Domain: {domain}")

        self.dns_event.clear()
        self.dns_result = None

        try:
            if connection_state and connection_state.get('connected'):
                self.connected = True
                self.current_server = connection_state.get('current_server')
                self.current_user = connection_state.get('current_user')
                self.sio = connection_state.get('sio')
                self.loop = connection_state.get('loop')

            if not self.connected:
                self.display.print_error("Not connected to server")
                return

            if not self.loop:
                self.display.print_error("Network loop not available")
                return

            future = asyncio.run_coroutine_threadsafe(self._resolve_dns(domain), self.loop)
            future.result(30)
        except Exception as e:
            self.display.print_error(f"DNS resolution error: {e}")
            return

        if not self.dns_event.wait(30):
            self.display.print_error("DNS resolution timeout")
            return

        if self.dns_result and self.dns_result.get('success'):
            content_hash = self.dns_result.get('content_hash')
            username = self.dns_result.get('username')
            verified = self.dns_result.get('verified', False)

            if self.via_controller:
                print(content_hash)
            else:
                self.display.print_success(f"DNS resolved successfully!")
                self.display.print_info(f"Hash: {content_hash}")
                self.display.print_info(f"Owner: {username}")
                self.display.print_info(f"Verified: {'Yes' if verified else 'No'}")
        else:
            if self.via_controller:
                print("DNS resolution failed")
            else:
                self.display.print_error("DNS resolution failed")

    async def _resolve_dns(self, domain):
        if not self.connected:
            self.display.print_error("Not connected to server")
            return

        await self.sio.emit('resolve_dns', {'domain': domain})

    def is_valid_domain(self, domain):
        import re
        pattern = r'^[a-z0-9-]+(\.[a-z0-9-]+)*$'
        return re.match(pattern, domain) is not None

    def handle_search(self, args):
        if not self.current_user:
            self.display.print_error("You need to be logged in to search")
            return

        if len(args) < 1:
            self.display.print_error("Usage: search <term> [--type TYPE] [--sort ORDER]")
            return

        query = args[0]
        content_type = "all"
        sort_by = "reputation"

        i = 1
        while i < len(args):
            if args[i] == '--type' and i+1 < len(args):
                content_type = args[i+1]
                i += 2
            elif args[i] == '--sort' and i+1 < len(args):
                sort_by = args[i+1]
                i += 2
            else:
                self.display.print_error(f"Unknown argument: {args[i]}")
                return

        if not self.via_controller:
            self.display.print_section(f"Search: '{query}'")
            self.display.print_info(f"Type: {content_type}")
            self.display.print_info(f"Sort by: {sort_by}")

        self.search_event.clear()
        self.search_result = None

        try:
            future = asyncio.run_coroutine_threadsafe(self._search_content(query, content_type, sort_by), self.loop)
            future.result(30)
        except Exception as e:
            self.display.print_error(f"Search error: {e}")
            return

        if not self.search_event.wait(30):
            self.display.print_error("Search timeout")
            return

        if self.search_result and 'error' not in self.search_result:
            results = self.search_result

            if not results:
                if self.via_controller:
                    print("No results found")
                else:
                    self.display.print_info("No results found")
                return

            if self.via_controller:
                for result in results:
                    print(f"{result.get('content_hash')}|{result.get('title')}|{result.get('username')}")
            else:
                table_data = []
                for result in results:
                    verified = "✓" if result.get('verified', False) else "⚠"
                    table_data.append([
                        verified,
                        result.get('title', 'No title'),
                        result.get('content_hash', '')[:16] + '...',
                        result.get('username', 'Unknown'),
                        result.get('mime_type', ''),
                        str(result.get('reputation', 100))
                    ])

                self.display.print_table(['✓', 'Title', 'Hash', 'Author', 'Type', 'Reputation'], table_data)
        else:
            if self.via_controller:
                print("Search failed")
            else:
                self.display.print_error("Search failed")

    async def _search_content(self, query, content_type, sort_by):
        if not self.connected:
            self.display.print_error("Not connected to server")
            return

        await self.sio.emit('search_content', {
            'query': query,
            'limit': 50,
            'content_type': content_type if content_type != "all" else "",
            'sort_by': sort_by
        })

    def handle_network(self, args):
        if not self.current_user:
            self.display.print_error("You need to be logged in to view network state")
            return

        if not self.via_controller:
            self.display.print_section("P2P Network State")

        self.network_event.clear()
        self.network_result = None

        try:
            future = asyncio.run_coroutine_threadsafe(self._get_network_state(), self.loop)
            future.result(30)
        except Exception as e:
            self.display.print_error(f"Network state error: {e}")
            return

        if not self.network_event.wait(30):
            self.display.print_error("Network state timeout")
            return

        if self.network_result and 'error' not in self.network_result:
            data = self.network_result

            if self.via_controller:
                print(f"Online nodes: {data.get('online_nodes', 0)}")
                print(f"Total content: {data.get('total_content', 0)}")
                print(f"Registered DNS: {data.get('total_dns', 0)}")
            else:
                self.display.print_info(f"Online nodes: {data.get('online_nodes', 0)}")
                self.display.print_info(f"Total content: {data.get('total_content', 0)}")
                self.display.print_info(f"Registered DNS: {data.get('total_dns', 0)}")

                node_types = data.get('node_types', {})
                if node_types:
                    self.display.print_section("Node Types")
                    for node_type, count in node_types.items():
                        self.display.print_info(f"{node_type}: {count}")

                with sqlite3.connect(self.db_path, timeout=10) as conn:
                    cursor = conn.cursor()
                    cursor.execute('SELECT node_id, address, node_type, reputation, status FROM cli_network_nodes ORDER BY last_seen DESC LIMIT 20')
                    rows = cursor.fetchall()

                    if rows:
                        table_data = []
                        for row in rows:
                            node_id, address, node_type, reputation, status = row
                            table_data.append([
                                node_id[:12] + '...',
                                address,
                                node_type,
                                str(reputation),
                                status
                            ])

                        self.display.print_table(['ID', 'Address', 'Type', 'Reputation', 'Status'], table_data)
        else:
            if self.via_controller:
                print("Failed to get network state")
            else:
                self.display.print_error("Failed to get network state")

    async def _get_network_state(self):
        if not self.connected:
            self.display.print_error("Not connected to server")
            return

        await self.sio.emit('get_network_state', {})

    def handle_stats(self, args):
        if self.via_controller:
            if self.stats_data['session_start'] > 0:
                session_duration = time.time() - self.stats_data['session_start']
                hours = int(session_duration // 3600)
                minutes = int((session_duration % 3600) // 60)
                seconds = int(session_duration % 60)
                session_time = f"{hours}h {minutes}m {seconds}s"
            else:
                session_time = "Not logged in"

            print(f"Session Time: {session_time}")
            print(f"Data Sent: {self.stats_data['data_sent'] / (1024*1024):.2f} MB")
            print(f"Data Received: {self.stats_data['data_received'] / (1024*1024):.2f} MB")
            print(f"Content Downloaded: {self.stats_data['content_downloaded']} files")
            print(f"Content Published: {self.stats_data['content_uploaded']} files")
            print(f"DNS Registered: {self.stats_data['dns_registered']} domains")
            print(f"PoW Solved: {self.stats_data['pow_solved']}")
            print(f"Total PoW Time: {int(self.stats_data['pow_time'])}s")
            print(f"Hashes Calculated: {self.stats_data['hashes_calculated']:,}")
            print(f"Content Reported: {self.stats_data['content_reported']}")
            print(f"Disk Space: {self.used_disk_space / (1024*1024):.2f}MB/{self.disk_quota / (1024*1024):.2f}MB")
            print(f"Reputation: {self.reputation}")
            print(f"User: {self.current_user or 'Not logged in'}")
            print(f"Server: {self.current_server or 'Not connected'}")
        else:
            self.display.print_section("Session Statistics")

            if self.stats_data['session_start'] > 0:
                session_duration = time.time() - self.stats_data['session_start']
                hours = int(session_duration // 3600)
                minutes = int((session_duration % 3600) // 60)
                seconds = int(session_duration % 60)
                session_time = f"{hours}h {minutes}m {seconds}s"
            else:
                session_time = "Not logged in"

            self.display.print_key_value("Session Time", session_time)
            self.display.print_key_value("Data Sent", f"{self.stats_data['data_sent'] / (1024*1024):.2f} MB")
            self.display.print_key_value("Data Received", f"{self.stats_data['data_received'] / (1024*1024):.2f} MB")
            self.display.print_key_value("Content Downloaded", f"{self.stats_data['content_downloaded']} files")
            self.display.print_key_value("Content Published", f"{self.stats_data['content_uploaded']} files")
            self.display.print_key_value("DNS Registered", f"{self.stats_data['dns_registered']} domains")
            self.display.print_key_value("PoW Solved", f"{self.stats_data['pow_solved']}")
            self.display.print_key_value("Total PoW Time", f"{int(self.stats_data['pow_time'])}s")
            self.display.print_key_value("Hashes Calculated", f"{self.stats_data['hashes_calculated']:,}")
            self.display.print_key_value("Content Reported", f"{self.stats_data['content_reported']}")
            self.display.print_key_value("Disk Space", f"{self.used_disk_space / (1024*1024):.2f}MB/{self.disk_quota / (1024*1024):.2f}MB")
            self.display.print_key_value("Reputation", f"{self.reputation}")
            self.display.print_key_value("User", f"{self.current_user or 'Not logged in'}")
            self.display.print_key_value("Server", f"{self.current_server or 'Not connected'}")

    def handle_report(self, args):
        if not self.current_user:
            self.display.print_error("You need to be logged in to report content")
            return

        if len(args) < 2:
            self.display.print_error("Usage: report <content_hash> <reported_user>")
            return

        content_hash = args[0]
        reported_user = args[1]

        if reported_user == self.current_user:
            self.display.print_error("You cannot report your own content")
            return

        if self.reputation < 20:
            self.display.print_error("Your reputation is too low to report content")
            return

        if not self.via_controller:
            self.display.print_section("Content Report")
            self.display.print_info(f"Hash: {content_hash}")
            self.display.print_info(f"Reported user: {reported_user}")
            self.display.print_info(f"Your reputation: {self.reputation}")

        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('''
SELECT COUNT(*) FROM cli_reports
WHERE reporter_user = ? AND content_hash = ?
                ''', (self.current_user, content_hash))
            count = cursor.fetchone()[0]
            if count > 0:
                self.display.print_error("You have already reported this content")
                return

        self.pending_report = (content_hash, reported_user)
        self.report_event.clear()
        self.report_result = None

        self.run_async(self.request_pow_challenge("report"))

        if not self.report_event.wait(300):
            self.display.print_error("Report timeout")
            if hasattr(self, 'pending_report'):
                del self.pending_report
            return

        if self.report_result and self.report_result.get('success'):
            if self.via_controller:
                print("Content reported successfully")
            else:
                self.display.print_success("Content reported successfully!")
        else:
            if self.via_controller:
                print("Report failed")
            else:
                self.display.print_error("Report failed")

    async def _report_content(self, content_hash, reported_user, pow_nonce, hashrate_observed):
        if not self.connected:
            return

        try:
            report_id = hashlib.sha256(f"{content_hash}{reported_user}{self.current_user}{time.time()}".encode()).hexdigest()

            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute('''
INSERT INTO cli_reports
(report_id, content_hash, reported_user, reporter_user, timestamp, status, reason)
VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (report_id, content_hash, reported_user, self.current_user, time.time(), 'pending', ''))
                conn.commit()

            await self.sio.emit('report_content', {
                'content_hash': content_hash,
                'reported_user': reported_user,
                'reporter': self.current_user,
                'pow_nonce': pow_nonce,
                'hashrate_observed': hashrate_observed
            })

        except Exception as e:
            self.display.print_error(f"Report sending error: {e}")

    def handle_security(self, args):
        if len(args) < 1:
            self.display.print_error("Usage: security <content_hash>")
            return

        content_hash = args[0]

        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('''
SELECT title, description, mime_type, username, signature, public_key, verified
FROM cli_content_cache WHERE content_hash = ?
                ''', (content_hash,))
            row = cursor.fetchone()

            if not row:
                self.display.print_error("Content not found in local cache")
                return

        title, description, mime_type, username, signature, public_key, verified = row

        content_file = os.path.join(self.crypto_dir, "content", f"{content_hash}.dat")
        if not os.path.exists(content_file):
            self.display.print_error("Content file not found")
            return

        with open(content_file, 'rb') as f:
            content = f.read()

        actual_hash = hashlib.sha256(content).hexdigest()
        integrity_ok = actual_hash == content_hash

        if self.via_controller:
            if not integrity_ok:
                print("CONTENT TAMPERED")
            elif verified:
                print("CONTENT VERIFIED")
            else:
                print("CONTENT NOT VERIFIED")

            print(f"Title: {title}")
            print(f"Author: {username}")
            print(f"Hash: {content_hash}")
            print(f"MIME Type: {mime_type}")
            print(f"Integrity: {'OK' if integrity_ok else 'COMPROMISED'}")
            print(f"Valid Signature: {'Yes' if verified else 'No'}")
            print(f"Size: {len(content)} bytes")
        else:
            self.display.print_section("Security Verification")

            if not integrity_ok:
                self.display.print_error("CONTENT TAMPERED")
            elif verified:
                self.display.print_success("CONTENT VERIFIED")
            else:
                self.display.print_warning("CONTENT NOT VERIFIED")

            self.display.print_key_value("Title", title)
            self.display.print_key_value("Author", username)
            self.display.print_key_value("Hash", content_hash)
            self.display.print_key_value("MIME Type", mime_type)
            self.display.print_key_value("Integrity", "OK" if integrity_ok else "COMPROMISED")
            self.display.print_key_value("Valid Signature", "Yes" if verified else "No")
            self.display.print_key_value("Size", f"{len(content)} bytes")

            if public_key:
                self.display.print_section("Author Public Key")
                print(public_key)

    def handle_servers(self, args):
        if self.via_controller:
            if not self.known_servers:
                print("No known servers")
                return

            for i, server in enumerate(self.known_servers, 1):
                status = "Connected" if server == self.current_server else "Available"
                print(f"{i}. {server} [{status}]")
        else:
            self.display.print_section("Known Servers")

            if not self.known_servers:
                self.display.print_info("No known servers")
                return

            table_data = []
            for i, server in enumerate(self.known_servers, 1):
                status = "✓ Connected" if server == self.current_server else "Available"
                table_data.append([str(i), server, status])

            self.display.print_table(['#', 'Address', 'Status'], table_data)

            action = self.display.get_input("\n[A]dd, [R]emove, [C]onnect, [Enter] to return: ").lower()

            if action == 'a':
                new_server = self.display.get_input("New server address: ")
                if new_server and new_server not in self.known_servers:
                    self.known_servers.append(new_server)
                    self.save_known_servers()
                    self.display.print_success(f"Server {new_server} added")

            elif action == 'r':
                try:
                    num = int(self.display.get_input("Server number to remove: "))
                    if 1 <= num <= len(self.known_servers):
                        removed = self.known_servers.pop(num-1)
                        self.save_known_servers()
                        self.display.print_success(f"Server {removed} removed")
                except ValueError:
                    pass

            elif action == 'c':
                try:
                    num = int(self.display.get_input("Server number to connect: "))
                    if 1 <= num <= len(self.known_servers):
                        server = self.known_servers[num-1]
                        self.handle_login([server, self.current_user or "", self.password or ""])
                except ValueError:
                    pass

    def handle_keys(self, args):
        if len(args) < 1:
            if self.via_controller:
                print("Available commands:")
                print("  keys generate  - Generate new keys")
                print("  keys export <path> - Export keys")
                print("  keys import <path> - Import keys")
                print("  keys show      - Show public key")
                return
            else:
                self.display.print_section("Key Management")
                self.display.print_info("Available commands:")
                self.display.print_info("  keys generate  - Generate new keys")
                self.display.print_info("  keys export <path> - Export keys")
                self.display.print_info("  keys import <path> - Import keys")
                self.display.print_info("  keys show      - Show public key")
                return

        subcommand = args[0]

        if subcommand == 'generate':
            if not self.no_cli and not self.via_controller:
                confirm = self.display.get_input("Generate new keys? (y/n): ").lower()
                if confirm != 'y':
                    return

            self.generate_keys()
            if self.via_controller:
                print("New keys generated and saved")
            else:
                self.display.print_success("New keys generated and saved")

        elif subcommand == 'export':
            if len(args) < 2:
                self.display.print_error("Usage: keys export <file_path>")
                return

            file_path = args[1]
            try:
                with open(file_path, "wb") as f:
                    f.write(self.private_key.private_bytes(
                                encoding=serialization.Encoding.PEM,
                                format=serialization.PrivateFormat.PKCS8,
                                encryption_algorithm=serialization.NoEncryption()
                            ))
                if self.via_controller:
                    print(f"Private key exported to: {file_path}")
                else:
                    self.display.print_success(f"Private key exported to: {file_path}")
            except Exception as e:
                self.display.print_error(f"Export failed: {e}")

        elif subcommand == 'import':
            if len(args) < 2:
                self.display.print_error("Usage: keys import <file_path>")
                return

            file_path = args[1]
            try:
                with open(file_path, "rb") as f:
                    self.private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
                self.public_key_pem = self.private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                )
                self.save_keys()
                if self.via_controller:
                    print("Keys imported successfully")
                else:
                    self.display.print_success("Keys imported successfully")
            except Exception as e:
                self.display.print_error(f"Import failed: {e}")

        elif subcommand == 'show':
            if self.public_key_pem:
                if self.via_controller:
                    print(self.public_key_pem.decode('utf-8'))
                else:
                    self.display.print_section("Public Key")
                    print(self.public_key_pem.decode('utf-8'))
            else:
                self.display.print_error("No public key available")

        else:
            self.display.print_error(f"Unknown subcommand: {subcommand}")

    def handle_sync(self, args):
        if not self.current_user:
            self.display.print_error("You need to be logged in to sync")
            return

        if not self.via_controller:
            self.display.print_section("Network Sync")

            self.display.print_info("Syncing known servers...")
            self.save_known_servers()

            self.display.print_info("Syncing local files...")
            self.run_async(self.sync_client_files())

            self.display.print_info("Getting network state...")
            self.run_async(self._get_network_state())

            self.display.print_success("Sync completed")
        else:
            self.save_known_servers()
            self.run_async(self.sync_client_files())
            self.run_async(self._get_network_state())
            print("Sync completed")

    def handle_history(self, args):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT command, timestamp, success, result FROM cli_history ORDER BY timestamp DESC LIMIT 20')
            rows = cursor.fetchall()

            if not rows:
                if self.via_controller:
                    print("No history available")
                else:
                    self.display.print_info("No history available")
                return

            if self.via_controller:
                for row in rows:
                    command, timestamp, success, result = row
                    time_str = datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
                    status = "SUCCESS" if success else "FAILED"
                    print(f"{time_str} [{status}] {command}")
            else:
                table_data = []
                for row in rows:
                    command, timestamp, success, result = row
                    time_str = datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
                    status = "✓" if success else "✗"
                    table_data.append([time_str, command[:30], status, result[:30] if result else ""])

                self.display.print_table(['Time', 'Command', 'Status', 'Result'], table_data)

    def handle_clear(self, args):
        if not self.no_cli and not self.via_controller:
            self.display.clear_screen()
            self.display.print_logo()
        elif not self.via_controller:
            os.system('cls' if platform.system() == 'Windows' else 'clear')

    def handle_help(self, args):
        if self.via_controller:
            print("Available Commands:")
            print("  login <server> <user> <pass> - Connect to P2P network")
            print("  logout - Disconnect from network")
            print("  upload <file> [options] - Upload file")
            print("  download <hash_or_url> - Download content")
            print("  dns-reg <domain> <hash> - Register DNS domain")
            print("  dns-res <domain> - Resolve DNS domain")
            print("  search <term> [options] - Search content")
            print("  network - View network state")
            print("  stats - View statistics")
            print("  report <hash> <user> - Report content")
            print("  security <hash> - Verify security")
            print("  servers - Manage servers")
            print("  keys [subcommand] - Manage cryptographic keys")
            print("  sync - Sync with network")
            print("  history - View command history")
            print("  clear - Clear screen")
            print("  help - Show this help")
            print("  exit/quit - Exit program")
        else:
            self.display.print_section("Available Commands")

            commands = [
                ("login <server> <user> <pass>", "Connect to P2P network"),
                ("logout", "Disconnect from network"),
                ("upload <file> [options]", "Upload file"),
                ("download <hash_or_url>", "Download content"),
                ("dns-reg <domain> <hash>", "Register DNS domain"),
                ("dns-res <domain>", "Resolve DNS domain"),
                ("search <term> [options]", "Search content"),
                ("network", "View network state"),
                ("stats", "View statistics"),
                ("report <hash> <user>", "Report content"),
                ("security <hash>", "Verify security"),
                ("servers", "Manage servers"),
                ("keys [subcommand]", "Manage cryptographic keys"),
                ("sync", "Sync with network"),
                ("history", "View command history"),
                ("clear", "Clear screen"),
                ("help", "Show this help"),
                ("exit/quit", "Exit program"),
            ]

            for cmd, desc in commands:
                self.display.print_key_value(cmd, desc)

            self.display.print_section("Upload Options")
            self.display.print_info("--title TITLE      Content title")
            self.display.print_info("--desc DESCRIPTION Content description")
            self.display.print_info("--mime MIME_TYPE   MIME type (ex: text/plain, image/jpeg)")

            self.display.print_section("Search Options")
            self.display.print_info("--type TYPE        Content type (all, image, video, document, text)")
            self.display.print_info("--sort ORDER       Sort by (reputation, recent, popular)")

    def handle_exit(self, args):
        self.is_running = False
        if self.connected and self.sio:
            self.run_async(self.sio.disconnect())
        if not self.via_controller:
            self.display.print_info("Exiting HPS CLI...")
        self.save_session_state()
        sys.exit(0)

    def run_async(self, coro, timeout=60):
        if not self.connected:
            self.display.print_error("Not connected to server")
            return None
        if not self.loop:
            self.display.print_error("Network loop not initialized")
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            return future.result(timeout)
        except Exception as e:
            self.display.print_error(f"Timeout or error in operation: {e}")
            return None

    def shutdown(self):
        self.is_running = False
        if self.sio and self.sio.connected:
            asyncio.run_coroutine_threadsafe(self.sio.disconnect(), self.loop)
        if self.network_thread:
            self.network_thread.join(timeout=5)

    def get_connection_state(self):
        return {
            'connected': self.connected,
            'current_server': self.current_server,
            'current_user': self.current_user,
            'sio': self.sio,
            'loop': self.loop,
            'session_id': self.session_id,
            'username': self.username,
            'reputation': self.reputation
        }

class HPSCommandLine(HPSClientCore):
    def __init__(self, no_cli=False, interactive_mode=False):
        super().__init__(no_cli=no_cli)
        self.interactive_mode = interactive_mode
        self.controller_monitor = ControllerFileMonitor(self, self.display)
        self.setup_command_handlers()

    def setup_command_handlers(self):
        self.command_handlers = {
            'login': self.handle_login,
            'logout': self.handle_logout,
            'upload': self.handle_upload,
            'download': self.handle_download,
            'dns-reg': self.handle_dns_register,
            'dns-res': self.handle_dns_resolve,
            'search': self.handle_search,
            'network': self.handle_network,
            'stats': self.handle_stats,
            'report': self.handle_report,
            'security': self.handle_security,
            'servers': self.handle_servers,
            'keys': self.handle_keys,
            'sync': self.handle_sync,
            'history': self.handle_history,
            'clear': self.handle_clear,
            'help': self.handle_help,
            'exit': self.handle_exit,
            'quit': self.handle_exit,
        }

    def save_history(self, command, success, result=""):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO cli_history (command, timestamp, success, result) VALUES (?, ?, ?, ?)',
                         (command, time.time(), 1 if success else 0, result))
            conn.commit()

    def run_interactive(self):
        self.controller_monitor.start_monitoring()

        self.display.clear_screen()
        self.display.print_logo()
        self.display.print_header("Interactive CLI Interface with Controller File")
        self.display.print_info("Type 'help' for available commands")
        self.display.print_info(f"Controller file: {self.controller_monitor.controller_file}")

        if self.current_user:
            self.display.print_status_bar(self.current_user, self.current_server, self.reputation)

        while True:
            try:
                if self.current_user:
                    prompt = f"{self.display.colors['green']}hps://{self.current_user}{self.display.colors['reset']}{self.display.colors['dim']}@{self.display.colors['reset']}{self.display.colors['blue']}{self.current_server}{self.display.colors['reset']} {self.display.colors['yellow']}»{self.display.colors['reset']} "
                else:
                    prompt = f"{self.display.colors['dim']}hps://disconnected{self.display.colors['reset']} {self.display.colors['yellow']}»{self.display.colors['reset']} "

                user_input = self.display.get_input(prompt).strip()

                if not user_input:
                    continue

                parts = user_input.split()
                command = parts[0].lower()
                args = parts[1:]

                if command in ['exit', 'quit']:
                    self.handle_exit(args)
                    break

                if command in self.command_handlers:
                    try:
                        self.command_handlers[command](args)
                        self.save_history(command, True)
                    except Exception as e:
                        self.display.print_error(f"Command error: {e}")
                        self.save_history(command, False, str(e))
                else:
                    self.display.print_error(f"Unknown command: {command}")

                if self.current_user:
                    self.display.print_status_bar(self.current_user, self.current_server, self.reputation)

            except KeyboardInterrupt:
                self.display.print_info("\nUse 'exit' to quit")
            except EOFError:
                break
            except Exception as e:
                self.display.print_error(f"Error: {e}")

        self.controller_monitor.stop_monitoring()

def main():
    parser = argparse.ArgumentParser(description='HPS CLI - Hsyst P2P Browser via Command Line')
    parser.add_argument('--no-cli', action='store_true', help='Non-interactive mode (command execution only)')

    args = parser.parse_args()

    cli = HPSCommandLine(no_cli=args.no_cli, interactive_mode=True)
    cli.run_interactive()

if __name__ == "__main__":
    main()